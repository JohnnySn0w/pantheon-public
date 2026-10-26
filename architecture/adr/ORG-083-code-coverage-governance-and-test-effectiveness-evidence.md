---
aliases: [ORG-083]
id: ORG-083
title: "Code coverage governance and test-effectiveness evidence"
status: "Ratified 2026-09-06 by the operator via Telegram 1895. Policy and evidence architecture only; no collector, validator, threshold, CI gate, or repository rollout exists yet."
date: 2026-08-31
author: Consultant
steward: orchestrator
hitl: Ratified
relates: ORG-038, ORG-041, ORG-065
build_disposition: needs-build
scoping_ticket: T-2954
---

# ORG-083 — Code coverage governance and test-effectiveness evidence

**Status:** **Ratified 2026-09-06 by the operator via Telegram 1895.** **HITL: Ratified.** This ADR creates no operational gate.

**Scope:** policy and evidence architecture only. Tool selection, schemas, validators, collectors, dependencies, CI wiring, storage, dashboards, repository baselines, thresholds, ratchets, and enforcement remain a separate ratification-gated build.

## Intent

Make the reach, effectiveness, and cost of Pantheon's tests measurable without substituting one percentage for assurance. A repository should be able to show what its tests reached, which failure modes they can detect, which real boundaries they exercise, what they cost, whether local and CI evidence agree, and why any exception exists.

## Context and fleet baseline

The survey behind this ADR covers every project row in `repos.yaml`: 13 live repositories, retired `pantheon-bridge`, and six ignored rows. Consultant directly inspected `pantheon-config@f114a947e538834830f9432a7e744f4b1429c6d5` and `codex-config@79f6b4ed8cc7328f38c3fefb1dea44ca3c501a61`; T-3377 inspected the head-owned repositories and resolved at `24d03d7162`. Absence below means the cited readable revision was searched. Retired and ignored rows were classified, not searched and then mislabeled absent.

At those revisions, no surveyed live code repository configures line, branch, function, changed-code, or mutation measurement, and none retains a measured flake history. Existing CI and boundary-test maturity varies widely. Therefore every repository begins this policy at `inventory_only`; a test suite or green build does not imply a measured baseline.

### Availability and evidence ledger

| Repository | Registry class / surveyed revision | Test and CI surface | Corrected evidence classification |
|---|---|---|---|
| `pantheon-config` | live; `f114a947e538834830f9432a7e744f4b1429c6d5` | Python and shell; self-hosted Python 3.11/3.12 CI | No quantitative tooling; production-subprocess, shell-suite, temporary-git, and integration evidence exist |
| `codex-config` | live; `79f6b4ed8cc7328f38c3fefb1dea44ca3c501a61` | Python 3.12 CI; pytest; unittest documented | No quantitative tooling; staged-hook subprocess, fixture-git, scrubbed-environment, and host-path boundary evidence exist |
| `echo-prismatic` | live; `9a27393e5883` | Rust workspace, Bun/Vitest frontend, active CI and daemon E2E | No quantitative tooling; the E2E test starts the real Echo binary, so production-subprocess and integration evidence are present; a shell-test framework is absent |
| `echo-recorder` | live; `0a3f5096720d` (`master`) | TypeScript/Bun application; no runner or CI | Live-code test-surface gap |
| `echo_vault` | live non-code; `c14a16b37245` | Obsidian architecture vault | Test dimensions not applicable while it remains non-code |
| `pantheon-pwa` | live; `7e2d94391de3` | Bun unit, Playwright E2E/visual, shell build smoke; no active CI | No quantitative tooling; real daemon/auth/UI boundary present; Playwright retry is retry policy, not measured flakiness |
| `revenue-project` | live non-code at survey; `8a2eaa286601` | Research/docs; no runner or CI | Explicit non-code/test-surface disposition; re-evaluate if executable work appears |
| `personal-site-astro` | live; `5234b908182e` | Astro/Node; CI builds only | Live-code test-surface gap; build success is not test-effectiveness evidence |
| `vibecamp-scheduler` | live; `4fe5c3684ee6` | Astro/Node deploy workflow; no test runner | Tolerated refresh-data failure is deploy tolerance, not retry or measured flakiness |
| `claude-config` | live; `3bdc5992acf7` | Python 3.12 CI; pytest | No quantitative tooling or configured real-boundary receipt identified |
| `codex-config-template` | live; `da38cfdcc8ed` | Python tests; reference mirror intentionally has no active CI | Test surface present; no quantitative tooling in the mirror |
| `exocortex-skeleton` | live; `5c919cf34857` | Local pytest suite; no CI | No quantitative tooling or configured boundary/flake evidence identified |
| `pantheon-public` | live; `ebae67b0e311` (`master`) | Python and shell tests; inactive workflow template only | Shell and live-acceptance evidence present; expected-failure manifest is quarantine/known-failure evidence, not measured flakiness |
| `pantheon-bridge` | retired | Not surveyed | Historical classification only; no revival implied |
| `excalidraw-diagram-skill` | ignored | Not surveyed | Non-project tool |
| `sentient-internal` | ignored | Not surveyed | Client/business material |
| `sentienthardware` | ignored | Not surveyed | Client/business material |
| `studio-operator` | ignored | Not surveyed | Firm/business material |
| `toys` | ignored | Not surveyed | Experiment/catchall |
| `vibecamp-trials-tracker` | ignored | Not surveyed | Non-Pantheon personal project |

The full command/file evidence and access classifications remain in [[T-2954]] and [[T-3377]]. The semantic corrections in this table are authoritative for this ADR: production subprocess is distinct from shell-suite execution, and retry, deploy tolerance, quarantine, and measured flakiness are four different things.

## Decision

Adopt a tool-neutral evidence portfolio with per-repository comparable baselines and ratchets, subordinate changed-code evidence, typed effectiveness and efficiency receipts, and validator-derived maturity. Do not set a universal fleet coverage percentage.

### 1. What coverage does and does not establish

Line coverage records whether configured source lines were reached. Branch coverage records tool-defined control-flow opportunities and observed transitions. Function coverage is a separate tool-defined dimension. These are **reachability measurements**: they do not prove correctness, requirement satisfaction, meaningful assertions, real-boundary behavior, defect detection, or cost-effectiveness.

Every live code repository must explicitly disposition line, branch, function, and changed-code coverage. Line and branch are required measurement targets once a suitable adapter exists. Function coverage is not a universal gate, because language and tool semantics differ, but omission still requires a named reason and authority. Changed-code coverage is a subordinate patch signal; it cannot replace project-level reachability or effectiveness evidence.

The controlled results of Inozemtseva and Holmes support caution about using coverage as a quality target, not a language-universal theorem. Tool documentation supplies vocabulary, not a Pantheon tool mandate. This is why adapters must preserve their exact denominator and semantics.

### 2. Root evidence contract

The downstream build must implement a versioned `RepositoryEvidenceContract`. Its root makes every specialized record reachable; prose-only or orphaned evidence is invalid.

```yaml
schema_version:
contract_id:
repository:
registry_revision:
evidence_revision:
collected_at:
repo_class: live_code | live_noncode | retired | ignored
arc_author:
fleet_steward_role:
repo_evidence_owner_role:
owner_resolution:
maturity:
target_maturity:
next_review:
dimension_dispositions: []
quantitative_measurements: []
changed_code_records: []
mutation_records: []
flake_observations: []
boundary_records: []
retry_policy_records: []
deploy_tolerance_records: []
test_surface_gaps: []
effectiveness_receipts: []
efficiency_records: []
parity_records: []
retention_records: []
generated_source_records: []
exclusion_records: []
waiver_records: []
maturity_deferrals: []
quarantine_records: []
artifact_records: []
```

Validator invariants:

1. Every ID is unique within the contract. Every reference resolves to one record of the declared type.
2. Referenced records agree on repository and immutable evidence revision unless the relationship explicitly names both revisions, as changed-code evidence does.
3. A `measured` disposition names at least one measurement ID. Changed-code, mutation, and flake measurements also name their applicable specialized record IDs.
4. Every relevant boundary has a boundary record; `not_exercised` requires a live deferral, while `not_applicable` requires applicability evidence and steward approval.
5. A retry, tolerated deploy failure, quarantine, or no-test state uses its dedicated record. None may be represented as a flake observation.
6. Every artifact reference resolves to an `ArtifactRecord` whose digest matches the retrieved bytes.
7. Missing, expired, unavailable, incomparable, or referentially invalid evidence cannot promote maturity.

The shared provenance fields bind evidence to the canonical repository name, the exact `repos.yaml` revision, an immutable repository revision, a UTC collection time, and resolved authority. Dirty working trees are not admissible measurement revisions.

### 3. Dimensions and quantitative measurements

A `DimensionDisposition` contains:

```yaml
dimension: line | branch | function | changed_code | mutation | flake
status: measured | unsupported | deferred | not_applicable | excluded
authority:
reason:
scope:
effective_revision:
measurement_ids: []
changed_code_record_id:
mutation_record_id:
flake_observation_ids: []
deferral_id:
exclusion_id:
re_evaluate_trigger:
```

`unsupported` and `deferred` require a live `MaturityDeferral`; `excluded` requires a live `ExclusionRecord`; `not_applicable` requires evidence and approval. An enum alone is never a completed disposition.

Each `QuantitativeMeasurementRecord` contains at least:

```yaml
measurement_id:
repository:
revision:
collected_at:
dimension:
unit:
collector: {adapter_id, tool_name, tool_version, command_fingerprint,
            config_digest, runner_fingerprint}
scope: {include_selectors, exclusion_ids, generated_source_ids, scope_fingerprint}
numerator:
denominator:
value:
baseline: {measurement_id, numerator, denominator, value}
current: {measurement_id, numerator, denominator, value}
threshold: {state, comparator, value, tolerance, authority_record}
ratchet: {direction, proposed_value, adopted_value, tolerance, authority_record}
comparability: {verdict, baseline_measurement_id, checked_fields,
                changed_fields, reason, reset_record}
changed_code_record_id:
mutation_record_id:
flake_observation_ids: []
artifact_id:
```

Threshold state is `unset | proposed | advisory | enforcing`; ratchet direction is `nondecreasing | nonincreasing | bounded`; comparability is `comparable | reset_required | refused`. A reviewed equivalence record is required to preserve a comparison after any material change to the runner, language substrate, adapter/tool version, command, configuration, source scope, exclusion set, generated-source treatment, denominator semantics, branch/function semantics, or test selection. Otherwise the baseline resets or comparison is refused. A favorable trend never survives a denominator change by default.

Changed-code evidence additionally binds `base_ref`, `base_revision`, `head_ref`, `head_revision`, `merge_base_revision`, diff tool/version/config, diff artifact/digest, eligible files/units, excluded units and exclusions, covered eligible units, and denominator rule. This ADR makes no claim that mutation work should be localized to changed lines.

A mutation record binds repository/revision, tool and operator-set versions/digests, seed, test selection, timeout policy, artifact, and counts for generated, killed, survived, no-coverage, timeout, error, equivalent, invalid, and excluded mutants. Every generated mutant receives one terminal disposition; equivalent, invalid, and excluded outcomes require reviewer evidence, and timeout/error are not kills. Mutation remains a bounded effectiveness proxy, not a correctness proof or universal gate.

A flake observation binds one immutable revision, materially equivalent environment and runner fingerprints, one test-set fingerprint, observation window, run count, pass/fail counts and sequence artifact, failure signatures, quarantine/retry links, numerator, denominator, rate, and artifact. Only inconsistent results across repeated comparable observations qualify as measured flakiness.

### 4. Boundaries, effectiveness, and efficiency

Each live code repository inventories relevant `production_subprocess`, `shell_suite`, `producer_consumer`, `ipc`, `network`, `storage`, `external_api`, and `ui_e2e` boundaries. A `BoundaryEvidenceRecord` identifies producer, transport, consumer, status (`exercised | simulated | not_exercised | not_applicable`), tests, preconditions, asserted effect, artifacts, owner, and any deferral. Shell execution and a real production subprocess are deliberately separate.

A `TestSurfaceGapRecord` is required when executable code has no runner or tests. It names the affected scope, owner, evidence, downstream restoration/build ticket, start, review trigger, target state, compensating evidence, and deferral. Silence is invalid.

An advisory or enforcing repository must carry at least one independently reviewed positive `EffectivenessReceipt` relevant to the governed scope, or a narrow live waiver with compensating evidence. The bounded receipt kinds are:

- `behavioral_falsification`: threat mutation/sabotage, observed red, restored green, and the assertion/effect that fired;
- `historical_fault_regression`: fault identity, failing pre-fix evidence, passing fixed evidence, and fault-specific assertion;
- `real_boundary`: producer, transport, consumer, real/simulated components, reached preconditions, and asserted delivered effect;
- `mutation`: linked mutation record, operator scope, survived-mutant review, and limitations;
- `quarantine_review`: linked quarantine, reproduction/restoration attempt, and disposition.

A quarantine review alone is not a positive effectiveness receipt. A coverage percentage alone never satisfies this obligation.

An `EfficiencyRecord` binds revision, environment/command/test-set fingerprints, observation window and sample count; local setup/runtime, CI queue/runner time, rerun and mutation cost, CPU/runner consumption and monetary cost where available; cadence, comparison baseline, comparability verdict, artifact, and limitations. Omitted cost classes are explicitly `not_applicable` or `unavailable` with reasons. Cost classes remain separate rather than being collapsed into one fleet score.

### 5. Parity, artifacts, and retention

`ParityRecord.verdict` is `exact | evidenced_equivalent | divergent_with_waiver | local_only | ci_only`. The record binds local and CI commands, environments, test sets, workflow revision, artifacts, differences, equivalence evidence, owner, waiver, and restoration plan.

- Baseline requires exact or evidenced-equivalent parity, or a live narrow waiver.
- Advisory and enforcing cannot depend on local-only or CI-only evidence.
- `divergent_with_waiver` expires with its waiver.
- Commands need not be byte-identical, but the governed scope and semantics must be evidenced equivalent.

An `ArtifactRecord` contains `artifact_id`, class, repository/revision, `created_at`, producer/tool/config identity, storage locator, media type, digest algorithm/digest, size, and sensitivity/access class.

Every artifact used for maturity, a threshold/ratchet, an exception, or enforcement has a `RetentionRecord`:

```yaml
retention_id:
artifact_id:
artifact_class:
created_at:
storage_locator:
digest_algorithm:
digest:
availability:
  state: verified_available | unavailable | deleted
  verified_at:
  retrieval_receipt_id:
retention_end:
  kind: timestamp | event
  value:
expiry_action: retain_tombstone | archive_with_tombstone | delete_with_tombstone
tombstone_artifact_id:
owner:
superseded_by:
```

Exactly one deterministic retention-end kind is selected. Availability means a retrieval was verified at `verified_at`, not merely that a locator was configured. Expiry, deletion, or unexpected unavailability creates a retained tombstone recording the prior digest, locator, reason, authority, action time, and last successful retrieval receipt. Evidence required to reconstruct a live maturity, ratchet, waiver, or enforcement decision cannot expire while that decision remains active.

### 6. Generated source, exclusions, waivers, deferrals, and quarantine

Generated code is not excluded automatically. A `GeneratedSourceRecord` names exact path selectors, source of truth, generator name/version/digest, generated revision/digest, risk acceptor, affected dimensions, treatment (`included | excluded | separate`), reason, any exclusion, and re-evaluation trigger.

Measurement exclusions, dimension waivers, repository maturity deferrals, and quarantines are separate record types. Each carries an ID, owner, reviewer/approver, reason, exact repository/path/dimension or test scope, start, expiry or typed trigger, restoration condition and plan, compensating evidence, decision/ticket link, active-state linkage, and typed expiry action. Renewal creates a new authority record; it is not an in-place date extension.

- `ExclusionRecord` changes a measurement scope/denominator and links to all affected measurements.
- `WaiverRecord` relaxes one named obligation and expires to `fail_closed`, `revert_to_advisory`, or `revert_to_inventory`.
- `MaturityDeferral` names current/target stages, blocked prerequisite, unsupported dimensions, downstream ticket, next action, review trigger, and on expiry emits fleet drift and refuses promotion.
- `QuarantineRecord` classifies `known_failure | suspected_flake | environment_specific | blocked_dependency`, identifies tests and evidence, links any same-revision flake observation and retry policy, records coverage/boundary impact, and owns restoration. Quarantine neither proves flakiness nor removes a test from denominator/effectiveness accounting without a separate exclusion.

`RetryPolicyRecord` records runner retry behavior. `DeployToleranceRecord` records an allowed deployment-step failure and its effect. Neither is flake evidence. These records, plus all exception and boundary records, are explicit root collections with referential-integrity validation.

### 7. Nonnumeric fleet minimums

Rejecting one fleet percentage does not mean accepting no fleet obligations. Every live code repository must have:

1. a resolved evidence owner and code/non-code applicability decision;
2. line, branch, function, and subordinate changed-code dispositions;
3. an inventory of relevant boundaries and a typed no-test gap where applicable;
4. local/CI parity and retention dispositions;
5. generated-source and exclusion treatment;
6. explicit effectiveness and efficiency status;
7. a target maturity and review trigger; and
8. a live deferral for any unsupported required dimension or stalled stage.

Unsupported and advisory states cannot persist silently: the deferral's review/expiry event produces fleet drift and blocks promotion. The fleet steward reviews age, renewal, and affected dimensions without turning exception counts into a second universal quota.

### 8. Validator-derived maturity

The validator, not a self-declared enum, derives the highest legal stage.

**`inventory_only` — the initial state for every repository.** Requires registry/revision/classification, authority resolution, runner/manifest/test/CI inventory, dimension and boundary dispositions, gaps, target stage, and review trigger. It forbids baseline, advisory, enforcement, or “coverage-compliant” claims. No repository can leave this state until the ratification-gated schema, validator, applicable adapters, artifact/retention checks, and parity evidence exist and are independently verified.

**`baseline`.** Requires validated collectors/adapters for measured dimensions, retained immutable artifacts, numerators/denominators, comparability fingerprints, parity, generated/exclusion treatment, line and branch measurements or approved dispositions, function/changed-code dispositions, and no expired exception. Baseline sets no enforcing threshold by default.

**`advisory`.** Requires comparable baseline/current evidence, an owner-proposed repository-specific threshold and ratchet, one positive independently reviewed effectiveness receipt or narrow waiver, an efficiency record, dry-run validator evidence, decision-artifact retention, and an exit/review trigger.

**`enforcing`.** Requires every advisory invariant, an adopted repository-specific threshold/ratchet, verified enforcement machinery and failure behavior, current comparable evidence, parity, repo-owner and fleet-steward approval, independently reviewed effectiveness evidence, and no expired exception. Coverage-only enforcement is invalid.

Missing, expired, unavailable, incomparable, or malformed evidence lowers eligibility or refuses evaluation according to the typed fallback. It never silently preserves a higher stage.

### 9. Governance and authority

The permanent fleet steward is the role key `orchestrator`, dynamically resolved through the canonical role/persona binding. Consultant is the T-2954 arc author and sole author of these draft bytes and the ratification surface; that grants no permanent waiver, ratchet, or enforcement authority.

Each repository contract explicitly names a `repo_evidence_owner_role` that has `rw` in the cited `repos.yaml` revision. With one eligible role it is the default candidate; with several, the repository remains inventory-only until the fleet steward records an assignment. The repository owner proposes ratchets and maintains evidence. The fleet steward approves ratchets, organization-level drift dispositions, and any waiver that weakens advisory/enforcing obligations. If owner and steward resolve to the same role, an independent reviewer receipt is mandatory. Reassignment records old/new roles, reason, effective revision, registry evidence, and approvals. Resolution failure blocks promotion and waiver approval.

### 10. Rollout and build boundary

Ratification alone changes no repository. Rollout is:

1. **Inventory:** keep all repositories `inventory_only`; preserve the survey and gaps.
2. **Build contract machinery:** versioned schemas, validator, ID/referential checks, role resolver, artifact/retention and exception expiry logic.
3. **Build bounded adapters and storage:** select tools only for an explicitly named pilot; retain exact provenance and semantics.
4. **Collect non-blocking baselines:** verify numerators, denominators, parity, comparability, effectiveness, and efficiency.
5. **Advisory pilot:** propose repository-specific thresholds/ratchets; run dry; independently review false positives, cost, and Goodhart pressure.
6. **Enforcement proposal:** only after verified machinery and evidence; name failure and escape behavior. No fleet-wide automatic promotion.
7. **Periodic review:** re-evaluate stale/incomparable evidence, exceptions, ownership, costs, and policy fit.

Tool choice, CI/pre-merge wiring, dashboards, alerts, storage backend, repository thresholds, and enforcement mechanisms are downstream implementation decisions. No advisory or enforcing declaration is valid before its collector, validator, artifact retention, and parity evidence are built and independently verified.

### 11. Escape hatches and anti-Goodhart controls

- No universal numeric target.
- Coverage cannot independently establish advisory or enforcing maturity.
- Tool, runner, scope, denominator, exclusions, and generated-source changes reset or refuse comparability unless equivalence is reviewed.
- Changed-code evidence remains subordinate.
- Mutation outcomes remain fully dispositioned.
- Quarantine, retry, deploy tolerance, and flake evidence stay distinct and visible.
- Exceptions require exact scope, authority, expiry/trigger, restoration, compensating evidence, and typed fallback; expired exceptions do not renew silently.
- Efficiency evidence keeps assurance cost visible.
- Periodic review may lower maturity when evidence becomes stale, unavailable, or incomparable.

## Alternatives considered

### A. One universal fleet coverage threshold — rejected

It is simple to communicate but unsupported by the fleet's absent baselines and heterogeneous languages/denominators. It rewards denominator manipulation and shallow reachability tests.

### B. Per-repository baselines and ratchets only — retained as a component

This respects substrate differences and protects trends, but remains hollow if percentages rise while tests cannot falsify behavior or exercise relevant boundaries.

### C. Changed-code coverage as the primary gate — rejected as primary, retained as subordinate

It is useful during patch review but can miss legacy risk, unchanged integration boundaries, and broad denominator changes. It does not replace project-level or effectiveness evidence.

### D. Typed evidence portfolio with repository baselines/ratchets and staged maturity — selected

It makes gaps and costs explicit and supports language-specific adapters without selecting tools here. The tradeoff is substantial schema, adapter, retention, and review machinery, so immediate enforcement is impossible and forbidden.

## Consequences

**Positive:** test reach, behavioral strength, boundary depth, parity, exceptions, and cost become separately auditable; tool changes cannot silently preserve favorable trends; repositories can mature at different rates without losing fleet minimums.

**Negative:** evidence production and review cost rises; the contract is intentionally richer than a coverage badge; adapters must preserve language-specific semantics; inventory-only may persist while the downstream build is pending.

**Residual risk:** effectiveness receipts can become ceremonial unless reviewers reproduce their threat mutation or real-boundary evidence. Exception volume can become another Goodhart surface. External empirical results are bounded by their studied languages, projects, tools, and fault populations and establish no Pantheon-wide numeric value.

## Source-to-claim and falsifier map

| Normative claim | Primary source | Applicability limit | Falsifier / re-open condition |
|---|---|---|---|
| Coverage alone is not an effectiveness proof | [Inozemtseva & Holmes, ICSE 2014](https://doi.org/10.1145/2568225.2568271) | Controlled study of five large Java systems; not a language-universal theorem | Replicated cross-language evidence that coverage alone robustly predicts relevant fault detection |
| Mutation can add effectiveness signal beyond coverage | [Just et al., FSE 2014](https://doi.org/10.1145/2635868.2635929) | Five studied applications; does not establish universal operators or thresholds | Pilot evidence that the selected mutants do not correlate with relevant real faults |
| Mutation scores are confounded by suite size/operator choices | [Papadakis et al., ICSE 2018](https://doi.org/10.1145/3180155.3180183) and [mutation survey](https://doi.org/10.1016/bs.adcom.2018.03.015) | Language, tool, operator, and corpus dependent | A governed adapter showing stable semantics across the relevant changes |
| Retry configuration is not measured flakiness | [Luo et al., FSE 2014](https://doi.org/10.1145/2635868.2635920) | Empirical causes in studied projects; governance distinction is an inference | Comparable fixed-revision observations showing the signal maps one-to-one to genuine flakes |
| Differential coverage detects some flakes but is not a complete metric | [Bell et al., ICSE 2018](https://doi.org/10.1145/3180155.3180164) | Studied implementation/ecosystem | Pilot shows no useful signal or validated completeness under Pantheon conditions |
| Repeated comparable runs are needed for a measured flake rate | Luo et al.; [Parry et al., ESE 2023](https://doi.org/10.1007/s10664-023-10307-w) | Study populations and definitions vary; policy is a design inference | A validated alternative distinguishes deterministic failures without repeated comparable observations |
| Branch denominators are tool-defined transitions | [Coverage.py branch documentation](https://coverage.readthedocs.io/en/stable/branch.html) | Python vocabulary only; not a tool mandate | An adapter's authoritative semantics differ, requiring its own typed denominator |
| Patch and project coverage are distinct scopes | [diff-cover documentation](https://github.com/Bachmann1234/diff_cover) and [Codecov documentation](https://docs.codecov.com/docs/frequently-asked-questions) | Tool vocabulary/examples, not evidence that patch scope is superior | Selected adapter cannot reproduce a stable base/head denominator |
| Defect evidence should prove the real failure red then restored green | `sop/standard/testing-depth-and-scope.md` Amendments G/H | Pantheon process rule; scope-proportionate | Reviewed evidence that the receipt is impossible or misleading for a specific test class |
| Machine validation and immutable provenance outrank prose-only compliance | P1, P3, P4 and D5 | Pantheon governance design inference | Validator cannot distinguish malformed/incomparable evidence from valid evidence |
| The current fleet has no configured quantitative coverage/mutation/flake machinery | [[T-2954]] direct survey and [[T-3377]] at `24d03d7162` | Exact surveyed revisions only | A cited file at those revisions showing a configured collector and retained result |

## Downstream build obligations

After ratification, the spawned build child must scope and independently review: the versioned record schemas; root reachability and referential validation; stage derivation; role resolution; artifact digests and retrieval receipts; retention/tombstone behavior; comparability; parity; exclusion/waiver/deferral/generated/quarantine expiry; bounded adapters; storage; baseline collection; effectiveness/efficiency receipts; and a non-enforcing pilot. It must add `build` and `org-083` labels in addition to its ratification lineage. It must not set fleet-wide numeric thresholds.

## Re-evaluate when

- A pilot cannot represent a real language/tool result or two adapters require incompatible semantics for a supposedly common dimension.
- A metric changes solely because runner, adapter, scope, denominator, generated-source treatment, or exclusions changed.
- Coverage rises while independently reviewed effectiveness evidence regresses.
- Collection materially harms local/CI efficiency.
- A repository remains unsupported/advisory beyond its deferral trigger.
- An exception cannot expire safely or retained evidence cannot reconstruct a live decision.
- Role or registry changes make ownership ambiguous.
- New replicated primary evidence materially changes the coverage/effectiveness, mutation, or flake conclusions.

## Ratification and provenance

This draft derives from T-2954, the T-3377 survey, direct repository-object audits, and three antagonistic proposal-review rounds. The third-boundary amendments make specialized records root-reachable, make retention availability observable, correct the HITL lifecycle, and require the future ratification Ask itself to identify the frozen bytes.

After this file is committed and independently reviewed at that exact commit, Consultant files the the operator ratification ticket through `tools/ticket.py file` with `--intent ratify`, the exact `--ratifiable-file pantheon-config:architecture/adr/ORG-083-code-coverage-governance-and-test-effectiveness-evidence.md`, and `--spawn-impl`. The `--ask` text—not only context—must name the frozen ADR commit and the independent exact-commit verdict/pointer. The generated implementation child then receives `build` and `org-083` labels. No ratification surface is filed while review is red.

## DECISIONS

- **Intent: measure test effectiveness and efficiency without substituting a percentage for assurance. Call: select a typed evidence portfolio with validator-derived maturity and repository-specific baselines/ratchets.** justified-via P1, P2, P3, P4, D5.
- **Intent: prevent hollow enforcing states. Call: require independently reviewed effectiveness evidence or a narrow live waiver; coverage alone cannot promote a repository.** justified-via P1, P3, P4, D5.
- **Intent: preserve comparisons across changing tools and denominators. Call: reset or refuse comparisons after material input changes unless equivalence is reviewed.** justified-via P1, P3, P4, D5.
- **Intent: keep exceptions visible and reversible. Call: type exclusions, waivers, deferrals, generated-source treatment, retry, deploy tolerance, quarantine, and flake separately, with expiry and restoration.** justified-via P1, P3, P4, D7.
- **Intent: avoid unsupported fleet generalization. Call: impose nonnumeric evidence obligations but no universal numeric threshold.** justified-via P2, P3, P6, D12.
- **Intent: make authority reconstructable without persona conflation. Call: role-key permanent stewardship and repository ownership; Consultant is this arc's author only.** justified-via P3, P4, P5, D7.
- **Intent: prevent policy prose from masquerading as machinery. Call: keep every repository inventory-only until the ratification-gated contract, validator, adapters, parity, retention, and pilot are built and independently verified.** justified-via P1, P4, D5, D6, D7.
- **Intent: preserve the ratification/build audit chain. Call: freeze and independently review the canonical commit, name it in the ratification Ask, spawn the gated implementation child, and label that child `build` plus `org-083`.** justified-via P3, P4, P5, D2, D7, D9.
