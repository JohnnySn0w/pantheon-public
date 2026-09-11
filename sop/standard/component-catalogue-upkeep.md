# Component catalogue upkeep — bounded periodic validation of the catalogue's records

**Status:** Drafted 2026-08-26 by Orchestrator (HO) per T-3180 D2-writer contract; implements ORG-055 (`architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md`) alongside the T-3181-landed component catalogue substrate. Rev-2 addresses T-3180 REBOUND (codex 2026-08-26). Awaiting the operator ratification.

## When to use this

When the **component catalogue's records** need periodic bounded validation for schema conformance, graph coherence, obligation currency, and cost/value balance — separately from any transaction that mutates records (which happens via extraction, assembly update, or direct component-record edit).

Upkeep is a **read-heavy scan** that emits typed candidates. It does NOT decide, mutate, decommission, or reassign. It routes decisions to their proper authority:

- **Pruning-candidate referrals** → `sop/standard/feature-usage-monitoring-and-pruning.md` (operator-gated). Upkeep does NOT define "unused" locally; it emits a referral requesting canonical measurement by the feature-usage SOP.
- **Stale-obligation candidates** → the ORG-061 obligation-index steward for the specific obligation ref.
- **Uneconomic/incompatible records** → the assembly-update SOP (if a lock must move) or the extraction/boundary-decision surface (if the split itself is in question).

**Do NOT use this SOP to** register a new component, publish a new lock, execute pruning, or measure feature usage. It is the SENSOR — a shallow "does this record still cohere against its authorities?" pass — not the ACTUATOR.

## Field-authority orientation

The `ComponentRecord` schema (`schemas/component-record.schema.json`, `additionalProperties: false` throughout) contains — under `upkeep` — only:

- `steward` — the named accountable identity
- `health_evidence` — string-ref array (pointers to evidence surfaces; upkeep verifies they resolve)
- `monitoring` — string-ref array (pointers to monitoring surfaces)
- `upgrade_rollback` — string-ref array
- `credential_rotation` — string-ref array
- `storage_retention` — string-ref array
- `cost` — sub-object with measurement fields
- `review_obligations` — string-ref array pointing to ORG-061 obligation-index entries

There is NO `operational_owner` field, NO `freshness_contract` field, NO local cadence value on the record. **Cadence, state, evidence-freshness, trigger conditions, and owner-currency for the record's obligations all live in the ORG-061 obligation index** that `review_obligations` references. Upkeep reads the index for those; it does not synthesize them from record fields that do not exist.

## Procedure

The bounded upkeep pass:

1. **Validate records, graph, generated indexes, manifests, and locks**:
   - Every `ComponentRecord` parses cleanly and satisfies its schema (`schemas/component-record.schema.json` with `additionalProperties: false`).
   - Every `RepositoryBoundaryDecision` parses cleanly and satisfies its schema (`schemas/repository-boundary-decision.schema.json` with `additionalProperties: false`).
   - The component graph (via `validate_component_graph`) has no dangling provides/requires, no capability namespace collisions, no missing `project_id` references.
   - The generated index (`components/index.json` via `generate_component_index`) matches the records byte-for-byte on regeneration.
   - Every referenced manifest parses; every referenced lock verifies against its manifest.

2. **Verify obligation-index entries via `review_obligations` refs — pointer + currency check**:
   - For each entry in a record's `upkeep.review_obligations`, resolve the ORG-061 obligation-index entry it points to.
   - The obligation-index entry is authoritative for: trigger conditions, cadence, state, evidence-freshness, and owner-currency.
   - An unresolvable ref (index entry missing) emits an `unresolvable_obligation_ref` candidate.
   - An index entry whose state is stale relative to its own declared cadence (all cadence semantics live in the index, not in the record or in this SOP) emits a `stale_obligation` candidate carrying the obligation ref and the index entry's own state.
   - An index entry whose owner has left / is empty / is "TBD" emits an `unowned_obligation` candidate carrying the ref.

3. **Verify pointer resolution for evidence/monitoring surfaces — WITHOUT copying live logs**:
   - Each `upkeep.health_evidence` and `upkeep.monitoring` string is a pointer. Upkeep verifies the pointer RESOLVES (URL/path/dashboard exists and is reachable).
   - Upkeep does NOT copy dashboard content, log bodies, or runbook text into the record. Pointer only. Copies rot; pointers age visibly.
   - An unreachable pointer emits an `unreachable_evidence_pointer` candidate.
   - Currency of the pointed-to evidence — how recently the dashboard/log was refreshed — is verified only when the corresponding `review_obligations` index entry declares a currency contract. Upkeep does not invent a currency contract where none exists in the obligation index.

4. **Check steward + measured cost currency**:
   - **Steward** — the record's `upkeep.steward` names an identity. Upkeep verifies the identity is still current (not departed, not "TBD"). An absent/departed steward emits an `unowned` candidate.
   - **Measured cost — currency check by `review_due_at` vs current time, NOT by field-population.** Schema validation in step 1 already guarantees every cost item has populated `measurement_source`, `owner`, `last_measured_at`, and `review_due_at` fields (`schemas/component-record.schema.json` §upkeep.cost item required-list). A tautological "does the field have data?" check therefore always passes; the observability signal for a stale measurement is `review_due_at < now`. For each cost item where `review_due_at` is in the past, emit a `stale_cost` candidate carrying the `cost_id`, `review_due_at`, and how overdue.
   - **Empty `upkeep.cost` array handling** — the schema permits an empty cost array (a record whose kind genuinely has no measurable cost). Upkeep does NOT emit `stale_cost` for an empty array; it emits `no_cost_declared` ONLY when the record's `kind` implies cost should be tracked (per the `kind` enum's cost-tracking-implied set as documented on the ORG-055 ADR or, absent that, per the `review_obligations` set — an obligation-index entry that names a cost-review obligation implies cost tracking). Absence-with-good-reason should not emit; absence-that-signals-a-gap should.
   - (An "operational owner" distinct from the steward is not a record field; the notion, if operationally meaningful for a specific component, lives in an obligation-index entry that `review_obligations` refs.)

5. **Emit typed candidates** — the pass emits a bounded, ordered list. Types include:
   - `unresolvable_obligation_ref` — `review_obligations` entry points to a missing obligation-index entry.
   - `stale_obligation` — obligation-index entry's own state is stale per its own cadence.
   - `unowned_obligation` — obligation-index entry's owner slot is empty/departed.
   - `unreachable_evidence_pointer` — `health_evidence` or `monitoring` pointer does not resolve.
   - `unowned` — record's `upkeep.steward` is empty/departed.
   - `stale_cost` — a `upkeep.cost` item's `review_due_at` is in the past (schema-required field, not a synthesized freshness contract).
   - `no_cost_declared` — record kind implies cost tracking, but `upkeep.cost` is empty (see step 4 for the kind-implies-cost predicate).
   - `uneconomic_signal` — measured cost trends exceed a cost-consequence threshold (a signal for feature-usage referral, NOT a decommission decision).
   - `incompatible` — a compatibility gate that was passing now refuses (schema drift, capability namespace collision surfaced by a downstream change).
   - `feature_usage_referral` — a signal that the feature-usage SOP's canonical measurement should re-evaluate this component's usage (see step 6).
   - `upkeep_cost_exceeded` — the upkeep pass ITSELF cost more than its declared value bar (raises ORG-055:R3).

6. **Route pruning-candidate signals to the feature-usage SOP as REFERRALS, NOT LOCAL DECISIONS**:
   - Upkeep may observe that a component has no lock reference in any current assembly. That is a SIGNAL, not a "usage measurement." The feature-usage SOP owns caller-classification, natural-experiment evidence, seasonal/DR discrimination, and cost measurement — this SOP does NOT re-implement any of that.
   - When upkeep observes a signal that would motivate pruning (no lock refs, `uneconomic_signal`, capability namespace deprecated by a downstream change), emit a `feature_usage_referral` candidate with the observed signal + a request for canonical feature-usage measurement.
   - The feature-usage SOP receives the referral, runs its canonical procedure, and produces the decommission decision (operator-gated). Upkeep's job ends at the referral.
   - Consequence: this SOP defines NO local "unused" threshold. A missing lock ref alone is not "unused" — it is a signal that the feature-usage SOP should measure.

7. **Preserve the feature-usage SOP's operator-gated capability-decommission authority** — this SOP has ZERO authority to remove a capability, retire a component, or delete a record. Every write action on a candidate routes upstream. The operator's ratification remains the sole authorization for capability-decommission.

8. **Measure its own cost and raise ORG-055:R3 when upkeep exceeds value**:
   - Each pass records its own wall-clock, tokens (if run inside an LLM session), and any operator attention it demanded.
   - If cumulative upkeep cost exceeds the operational value it produces over a bounded window (default 3 months), emit an `upkeep_cost_exceeded` candidate and surface ORG-055:R3 for re-evaluation.
   - **No timer is presumed.** T-3184 measures a first run before recommending cadence.

## Cadence

There is currently NO presumed cadence for this SOP. T-3184 (the T-2415-arc sibling ticket) measures a first upkeep run's cost + candidate yield, and cadence — when it exists — is owned by the ORG-061 obligation-index entry that represents "run catalogue upkeep." Until that obligation-index entry is authored (post-T-3184), upkeep runs on-demand (operator invocation) or opportunistically (folded into an unrelated dispatch on the same catalogue).

**Note:** this SOP does NOT write cadence onto the ORG-055 ADR's DECISIONS line or onto any record. Cadence is exclusively an obligation-index concern; a prior draft's suggestion otherwise was in error and is retracted in this revision.

## ORG-055 re-evaluation triggers

This SOP explicitly watches for the ORG-055 re-evaluation triggers R1–R5. When upkeep observes:

- **R1** — a single seed manifest resolves ≥3 organ repos, evaluate whether the seed-manifest surface itself needs its own catalogue treatment.
- **R2** — a submodule is proposed as the only source of dependency truth, reject the proposal and surface for ORG-055 re-evaluation.
- **R3** — catalogue upkeep exceeds the operational value it produces (auto-emitted per step 8), surface for ORG-055 re-evaluation.
- **R4** — ORG-046 is ratified, rejected, or materially redesigned, re-examine the `LogicalRepoResolver` adapter seam for continued substitutability.
- **R5** — binary/firmware/model artifacts require a registry not expressible through Git references, surface for ORG-055 re-evaluation.

Any trigger fires as an on-record obligation-index entry + a ledger reference; none of them force an immediate structural change.

## Common failure modes

- **Reading fields the schema does not have** — upkeep asserts `operational_owner` or `freshness_contract` on the record. Those fields do not exist; the record refuses to carry them (`additionalProperties: false`). Fix: every currency/cadence/owner-currency check resolves through `review_obligations` → obligation-index entry.

- **Defining "unused" locally** — upkeep tags a component as "unused" because no lock references it, and cascades directly to a pruning decision. This bypasses feature-usage's canonical measurement (which classifies seasonal/DR features correctly, weighs natural-experiment evidence, and preserves the operator-gated authority). Fix: NEVER decide unused locally; ALWAYS emit a `feature_usage_referral` and let feature-usage measure.

- **Copying evidence content instead of pointer-checking** — the record grows unbounded and stale content masquerades as fresh. Fix: pointer resolution only, never copy the body.

- **Writing cadence onto a record or an ADR** — an operator invents a "review annually" default and writes it into `upkeep.cost` or ORG-055's DECISIONS section. Cadence lives ONLY in the ORG-061 obligation index. Fix: create/reference an obligation-index entry with the cadence; store its ref in `review_obligations`.

- **Cost measurement omitted** — the pass runs but does not record its own cost. R3 becomes unobservable; upkeep grows uneconomic silently. Fix: step 8 is not optional.

- **Candidate types collapsed to a boolean** — the pass emits "issues" without typing them. Routing becomes impossible; every candidate needs manual triage. Fix: preserve the typed candidate shape from step 5.

- **Obligation-index entry orphaned by design** — a component's obligation review is genuinely orphaned (former owner departed, no replacement named). Fix: `unowned_obligation` candidate routes to whoever owns the parent project's steward slot; if that is also empty, escalate to the operator via the ticket surface.

## Boundary

This SOP covers **periodic bounded validation of the component catalogue's records against their schemas, graph coherence, obligation-index refs, and cost/value balance**. It does NOT cover:

- **Component-record authoring** — new records enter via extraction (`repository-extraction.md`) or direct edit under D2-writer authority.
- **Assembly-lock changes** — that's `assembly-update.md`.
- **Feature-usage measurement** — canonical caller/behavior/natural-experiment/cost methodology owned by `feature-usage-monitoring-and-pruning.md`; not duplicated here.
- **Capability decommission** — operator-gated via `feature-usage-monitoring-and-pruning.md`.
- **Cadence/state/evidence storage** — owned by the ORG-061 obligation index; not stored on records, not stored in ADR DECISIONS, not stored in this SOP.
- **Ongoing health monitoring** — health dashboards fire on their own cadence; upkeep only verifies the pointers resolve, does not run them.

## Cross-references

- `architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md` — the ratified authority for catalogue upkeep bounds.
- `architecture/adr/ORG-061-*.md` — owned obligation index (owns trigger/steward/cadence/state/evidence; upkeep reads via `review_obligations` refs).
- `sop/standard/repository-extraction.md` — how new component records enter the catalogue.
- `sop/standard/assembly-update.md` — where lock-transaction work happens (upkeep never lock-mutates).
- `sop/standard/feature-usage-monitoring-and-pruning.md` — operator-gated decommission authority and canonical usage-measurement procedure; do NOT duplicate its procedure here.
- `tools/component_catalogue.py` — the runtime implementation (T-3181 landing).
- `schemas/component-record.schema.json` — the canonical record schema (`additionalProperties: false`).
- `schemas/repository-boundary-decision.schema.json` — the boundary-decision schema.
- `components/index.json` — the generated deterministic index.
- T-3184 — cadence-measurement sibling ticket (owns the eventual cadence obligation-index entry).
- ORG-055:R1–R5 — re-evaluation triggers watched in the dedicated section above.
- `tickets/plans/T-2415-implementation-plan.md` — parent plan authority.
