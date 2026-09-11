---
id: ORG-084
title: Executable runtime contract projection
status: "Adopted 2026-09-07 by Cairn under D3 during the T-3388 bounded autonomy experiment, after independent substantive review of contract candidate ff05165e256a0de17ac17c241a3b911d62f7fbad. Contract and migration inventory only; no schema validator, generator, CI gate, collector, deployment, or runtime adoption exists yet."
date: 2026-08-31
hitl: Autonomous
build_disposition: needs-build
scoping_ticket: T-2956
---

# ORG-084 — Executable runtime contract projection

## Intent

Make Pantheon's end-to-end runtime architecture traversable and falsifiable without creating another source of truth. Human-facing architecture remains explanatory; a digest-bound runtime-trace profile lets agents and CI reject missing dependencies, ambiguous ownership, incomplete delivery claims, unreachable rollback, stale evidence, and lifecycle traces that skip a required boundary.

This ADR responds to T-2953's repeated component-green/full-path-red failures. It does not declare the current fleet healthy, migrated, or even observed. It specifies the model against which later read-only inventory, implementation, and production-shaped evidence can be checked.

## Decision

Adopt `pantheon-runtime-trace-profile/v1`: an orthogonal operational-trace profile that composes the existing architecture atlas rather than extending its authority.

- The atlas remains a derived navigation surface over project, component, organ, binding, dependency, stage, and obligation authorities.
- [[ORG-080]] remains sole authority for component contract fields, including component identity, dependency DAG, bindings, lifecycle operations, receipts, and rollback.
- [[ORG-081]] remains authority for contract adoption and per-leg coverage. It is not runtime truth.
- [[ORG-068]] remains authority for lifecycle obligations.
- `instance.yaml` remains desired-state authority for the fields its code validator admits; desired state is not installed or observed state.
- [[ORG-050]], [[ORG-052]], [[ORG-057]], [[ORG-063]], [[ORG-064]], [[ORG-079]], and [[ORG-082]] retain their identity, capability, continuity, isolation, scheduling, completeness, and broker-boundary fields.
- [[ORG-083]] is a foreign evidence seam only. It remains proposed and unbuilt; this ADR neither ratifies it nor creates its machinery.
- ORG-084 alone owns runtime-trace vocabulary: atomic fact planes, delivery milestones, trace applicability, operational pre/postconditions, failure exits, and the rules for composing authority references into a non-authoritative trace.

No projection may copy a field and become its writer. When two sources claim authority over the same field, validation refuses; document order and precedence never choose a winner. Generated graphs and operator sheets are disposable views and cannot write back to an ADR, component contract, manifest, registry, or observed-state store.

## Authority and fact planes

Canonical authorities and non-authoritative sources are structurally distinct closed unions.

`CanonicalAuthorityKind` is:

```text
ratified_contract | desired_manifest | component_contract |
system_registry | runtime_trace_contract
```

Only a `CanonicalAuthorityRef` may appear in `FieldOwnership.sole_authority_ref`. A proposed or rebounded design is not a canonical authority until its own lifecycle changes.

`NonAuthoritySourceKind` is:

```text
superseded_contract | proposed_design | rebounded_design | landed_mechanism |
installed_observation | runtime_observation | retained_evidence |
generated_view | open_work
```

Every `ProjectedFact` is atomic and occupies exactly one `FactPlane`:

```text
desired | declared_contract | landed_mechanism | installed_adopted |
runtime_observed | retained_evidence | generated_view | open_work
```

`desired` and `declared_contract` facts require canonical authority sources. Every other plane requires a non-authoritative source. A fact has one subject, field, plane, source, value digest, observation or source time where applicable, freshness rule, and limitations. No fact overwrites a fact in another plane. In particular:

- code existence does not prove installation;
- installation does not prove current runtime state;
- a runtime observation does not rewrite desired state or a contract;
- a ticket is open-work evidence, never implementation or runtime truth;
- a test or retained receipt is evidence, never architectural intent;
- generated output is never authority.

## Atlas composition

The profile imports, without redefining, the atlas `AtlasIdentityRef`, `AtlasAuthorityRef`, and six-field `SourcePin`:

```yaml
SourcePin:
  source_class: repo-document | evidence-snapshot
  canonical_ref: string
  authority_path: string | null
  document_sha256: sha256
  authority_digest: sha256
  field_path: string
```

A `RuntimeIdentityMapping` maps one runtime node to either one resolvable atlas identity or a typed deferral. A deferred identity carries an owning ticket and cannot be emitted into an atlas view. A resolved mapping must pass the atlas's own authority loader at the frozen source revision. Runtime IDs never masquerade as atlas IDs.

`RuntimeSourcePin` wraps one byte-identical atlas `SourcePin` plus only `fact_plane`, `freshness_rule`, and `limitations`. Those three fields describe projection use; they do not alter atlas authority.

The current atlas has no machine-readable portfolio index and its canonical project/component record sets are substantially unmaterialized. Therefore the initial profile below is `mode: inventory`, its runtime identities are explicitly deferred, and `atlas_emit_allowed` is false. Executable mode refuses any deferred identity.

## Machine-readable companion contract

The normative companion is YAML or equivalent JSON. YAML aliases, implicit timestamps, custom tags, duplicate keys, non-UTF-8 input, and non-string mapping keys are forbidden. Digests use canonical JSON: UTF-8, sorted keys, no insignificant whitespace, arrays order-preserving, and a terminal newline.

### Root and cardinalities

```yaml
schema: pantheon-runtime-trace-profile/v1        # exactly one
mode: inventory | executable                    # exactly one
profile_id: stable-id                           # exactly one
frozen_sources: [FrozenSourceRef+]               # one or more
atlas_compat: AtlasCompatibility                 # exactly one
canonical_authorities: [CanonicalAuthorityRef+] # one or more
non_authority_sources: [NonAuthoritySourceRef+] # one or more
stewards: [StewardRef+]                           # one or more
runtime_actors: [RuntimeActorRef+]                # one or more
field_ownership: [FieldOwnership+]              # one or more
facts: [ProjectedFact+]                          # one or more
applicability: [ApplicabilityDisposition+]      # one or more
conditions: [Condition+]                         # one or more
failures: [FailureClassRecord+]                 # one or more
receipts: [ReceiptRequirement+]                 # one or more
boundaries: [SecurityBoundary+]                 # one or more
rollbacks: [RollbackDisposition+]               # one or more
object_contracts: [ObjectContract+]             # exactly one per node/edge
nodes: [RuntimeNodeProjection+]                 # one or more
edges: [RuntimeEdgeProjection+]                 # one or more
traces: [LifecycleTrace+]                       # one or more
architectural_exercise_refs: [ArchitecturalExerciseRef*]
operator_sheets: [OperatorSheetProvenance*]
migration: [MigrationLedgerRow+]                 # one or more
```

All IDs are globally unique within their type and match `^[a-z][a-z0-9._:-]{2,127}$`. Every reference resolves once and to the required type. `steward_ref` resolves only in `stewards`; `runtime_actor_ref` resolves only in `runtime_actors`. Unknown fields, unknown enum members, duplicate IDs, duplicate keys, dangling references, and wrong-type references refuse validation.

`RuntimeNodeProjection` has exactly `node_id`, `layer`, `kind`, `atlas_identity_mapping_ref`, `contract_ref`, `fact_refs`, and `applicability_refs`. `RuntimeEdgeProjection` adds exactly `edge_id`, `source_node_id`, `target_node_id`, `relation`, `relation_class`, `contract_ref`, `fact_refs`, and `applicability_refs`. `ObjectContract` has exactly `contract_id`, `subject_id`, `authoritative_input_refs`, `steward_ref`, `runtime_actor_ref`, `precondition_refs`, `postcondition_refs`, `failure_refs`, `receipt_refs`, `boundary_refs`, and `rollback_ref`.

`StewardRef` has exactly `steward_id`, `identity_authority_ref`, and `responsibility`. `RuntimeActorRef` has exactly `actor_id`, `identity_authority_ref`, `capability_authority_ref`, and `status`; `status` is `projected | installed | observed`, and the latter two require matching facts in those planes.

Every digest-valued field uses a typed `DigestDisposition`: either `{status: materialized, sha256: <64 lowercase hex>}` or `{status: deferred, owner_ticket: T-NNNN, reason: <nonempty>}`. Deferred digests are valid only in `inventory` mode and make the inventory non-emittable; they are not fake hashes. `executable` mode requires every digest materialized and current.

`FrozenSourceRef` is a closed union. `repo_revision` has exactly `source_id`, `source_kind`, `repo`, and an exact 40-hex `revision`. `enclosing_document` has exactly `source_id`, `source_kind`, `repo`, `path`, `reviewed_revision`, and `document_digest`; its exact revision and byte digest come from the review/adoption envelope supplied to the validator. The latter exists only to break an impossible self-hash/self-commit cycle. It resolves to the current input bytes but remains ineligible as authority until the envelope records adoption. A null `reviewed_revision` and deferred digest are permitted only in blocked inventory mode. Missing envelope identity blocks inventory promotion and executable mode.

`LifecycleTrace` has exactly `trace_id`, `trace_kind`, `graph_digest`, `schema_digest`, `source_refs`, `start_state`, `end_state`, `initial_condition_refs`, `ordered_edge_ids`, `applicability_refs`, `required_delivery_milestones`, `failure_exits`, `rollback_edge_ids`, and `limitations`. Validation carries a monotonic set of established conditions: initialize it from `initial_condition_refs`, require each edge's preconditions to be a subset, then union that edge's postconditions. The immediately prior edge need not alone imply the next edge because independent prerequisites and converging inputs may have been established earlier. A condition may be initial only when an authoritative input and receipt named by the trace prove it.

`relation_class` is the closed enum:

```text
dependency | order | requires | produces | transports | consumes | hands_off |
observes | secures | continuity_transfer | degrades | evidenced_by | rollback
```

Dependency, order, and requires subgraphs must be acyclic. Degradation, recovery, continuity-transfer, and rollback feedback is permitted only when every loop carries a bounded termination or explicit break condition in its rollback/continuity contract.

### Delivery, applicability, and rollback

Delivery milestones are distinct and monotonic only when evidence for each prior stage is present:

```text
fetched | durably_queued | submitted | turn_started | consumed_acked |
externally_responded | asserted_effect_proven
```

No milestone implies a later one. Maildir `cur/` proves handler completion, not response or effect. A wake receipt proves a signal attempt, not consumption. A turn-start receipt proves neither completion nor external response.

Every lifecycle leg and cross-layer boundary has one `ApplicabilityDisposition`:

```yaml
obligation_id: stable-id
scope_ref: node-or-edge-or-trace-id
kind: lifecycle_leg | identity | security | credential | receiver |
      delivery | health | continuity | rollback
name: string
disposition: applicable | waived | not_applicable
authority_ref: CanonicalAuthorityRef
reason: string
waiver_ref: null | authority-bearing-record-ref
expires_at_or_trigger: null | string
```

Silence is invalid. `waived` requires a live authority-bearing waiver and deterministic expiry/trigger. `not_applicable` requires an authority and a trace-kind-specific reason; it cannot mean “not implemented.”

Every object has one `RollbackDisposition`:

```text
reversible_inverse | compensating_recovery | refused_before_mutation |
non_mutating | not_applicable
```

Every mutating failure state must reach a receipt-anchored reversible inverse or explicitly authorized compensating recovery. Otherwise mutation is refused before it begins. Rollback removes only receipt-proven owned state and preserves user-owned state.

### Schema evolution

`v1` accepts only `schema: pantheon-runtime-trace-profile/v1`. An unknown major version refuses. A future migration must be a named pure transform from one exact schema digest to another, retain the original artifact digest, emit a migration receipt, preserve every fact plane and authority source, and be idempotent. A migration may add a new fact; it may not reinterpret or merge an old fact. Executable mode additionally requires no deferred atlas identity, no stale source/evidence, no waived obligation beyond its expiry, and no untraced applicable object.

## Complete initial inventory fixture

The following is a schema-complete, validator-consumable inventory fixture. A conforming validator accepts its structure and returns `inventory_blocked` because its candidate self-source and digest dispositions are deliberately deferred; it cannot emit an atlas view, operator sheet, executable trace, or adoption claim. Compact flow records are ordinary YAML mappings, not prose abbreviations.

```yaml
schema: pantheon-runtime-trace-profile/v1
mode: inventory
profile_id: pantheon-persona-runtime-inventory

frozen_sources:
  - {source_id: fs:pantheon, source_kind: repo_revision, repo: pantheon-config, revision: 165a6a6b47dcf47485013a72f73d7a311fed304c}
  - {source_id: fs:codex, source_kind: repo_revision, repo: codex-config, revision: 79f6b4ed8cc7328f38c3fefb1dea44ca3c501a61}
  - {source_id: fs:org084-candidate, source_kind: enclosing_document, repo: pantheon-config, path: architecture/adr/ORG-084-executable-runtime-contract-projection.md, reviewed_revision: null, document_digest: {status: deferred, owner_ticket: T-2956, reason: awaiting-exact-adoption-envelope}}

atlas_compat:
  schema_ref: schemas/architecture-atlas-view.schema.json
  source_pin_shape: [source_class, canonical_ref, authority_path, document_sha256, authority_digest, field_path]
  atlas_emit_allowed: false
  identity_mappings:
    - {mapping_id: map:runtime-profile, runtime_node_pattern: "n:*", status: deferred, atlas_identity_ref: null, owner_ticket: T-2667}

canonical_authorities:
  - {authority_id: a:manifest, kind: desired_manifest, source: fs:pantheon, path: instance.yaml}
  - {authority_id: a:org050, kind: ratified_contract, source: fs:pantheon, path: adr/ORG-050-security-and-identity-contract.md}
  - {authority_id: a:org052, kind: ratified_contract, source: fs:pantheon, path: adr/ORG-052-harness-neutral-host-capability-broker.md}
  - {authority_id: a:org057, kind: ratified_contract, source: fs:pantheon, path: adr/ORG-057-continuity-and-harness-independent-selfhood.md}
  - {authority_id: a:org068, kind: ratified_contract, source: fs:pantheon, path: adr/ORG-068-universal-system-lifecycle-contract.md}
  - {authority_id: a:org079, kind: ratified_contract, source: fs:pantheon, path: adr/ORG-079-l1-install-completeness-contract.md}
  - {authority_id: a:org080, kind: component_contract, source: fs:pantheon, path: adr/ORG-080-germination-engine.md}
  - {authority_id: a:org081, kind: system_registry, source: fs:pantheon, path: adr/ORG-081-system-registry.md}
  - {authority_id: a:org084, kind: runtime_trace_contract, source: fs:org084-candidate, path: architecture/adr/ORG-084-executable-runtime-contract-projection.md, activation: after-adoption-only}

non_authority_sources:
  - {source_id: na:installer-code, kind: landed_mechanism, source: fs:pantheon, path: tools/persona_installer.py}
  - {source_id: na:runtime-code, kind: landed_mechanism, source: fs:pantheon, path: tools/runtime_controller.py}
  - {source_id: na:delivery-code, kind: landed_mechanism, source: fs:pantheon, path: tools/harness_adapter/delivery.py}
  - {source_id: na:bridge-code, kind: landed_mechanism, source: fs:pantheon, path: tools/codex_telegram_bridge.py}
  - {source_id: na:poller-code, kind: landed_mechanism, source: fs:codex, path: codex_telegram/poller.py}
  - {source_id: na:mailbox-code, kind: landed_mechanism, source: fs:pantheon, path: tools/mailbox.py}
  - {source_id: na:health-code, kind: landed_mechanism, source: fs:pantheon, path: tools/host_control/health_dbus.py}
  - {source_id: na:t2953, kind: open_work, source: fs:pantheon, path: tickets/inbox/T-2953.md}
  - {source_id: na:org076, kind: rebounded_design, source: fs:pantheon, path: adr/ORG-076-fleet-health-and-repair-system.md}
  - {source_id: na:org083, kind: proposed_design, source: fs:pantheon, path: adr/ORG-083-code-coverage-governance-and-test-effectiveness-evidence.md}

stewards:
  - {steward_id: orchestrator, identity_authority_ref: a:org050, responsibility: desired-state-and-runtime-contract-stewardship}
  - {steward_id: recovery, identity_authority_ref: a:org050, responsibility: continuity-health-and-bounded-recovery}

runtime_actors:
  - {actor_id: active-harness, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: codex-poller, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: continuity-controller, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: harness-adapter, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: health-collector, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: identity-resolver, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: mailbox-consumer, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: mailbox-router, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: mailbox-wake, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: manifest-validator, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: persona-installer, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: receipt-store, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: receiver-handoff-adapter, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: rollback-controller, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}
  - {actor_id: runtime-controller, identity_authority_ref: a:org050, capability_authority_ref: a:org052, status: projected}

field_ownership:
  - {ownership_id: own:desired, subject_pattern: "n:manifest.*", field_pattern: desired.*, sole_authority_ref: a:manifest, conflict_policy: refuse}
  - {ownership_id: own:identity, subject_pattern: "n:identity", field_pattern: "*", sole_authority_ref: a:org050, conflict_policy: refuse}
  - {ownership_id: own:lifecycle, subject_pattern: "app:lifecycle.*", field_pattern: "*", sole_authority_ref: a:org068, conflict_policy: refuse}
  - {ownership_id: own:component, subject_pattern: "component:*", field_pattern: "*", sole_authority_ref: a:org080, conflict_policy: refuse}
  - {ownership_id: own:coverage, subject_pattern: "coverage:*", field_pattern: "*", sole_authority_ref: a:org081, conflict_policy: refuse}
  - {ownership_id: own:trace, subject_pattern: "trace:*|n:*|e:*", field_pattern: trace.*, sole_authority_ref: a:org084, conflict_policy: refuse}

facts:
  - {fact_id: fact:manifest, subject_id: n:manifest, field: desired.runtime, plane: desired, canonical_source_ref: a:manifest, non_authority_source_ref: null, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: source-digest-exact, limitations: deployed-state-unverified}
  - {fact_id: fact:identity, subject_id: n:identity, field: declared.identity, plane: declared_contract, canonical_source_ref: a:org050, non_authority_source_ref: null, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: source-digest-exact, limitations: runtime-identity-unverified}
  - {fact_id: fact:installer, subject_id: n:installer, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:installer-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: installation-unverified}
  - {fact_id: fact:runtime, subject_id: n:runtime-controller, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:runtime-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: deployment-unverified}
  - {fact_id: fact:receiver, subject_id: n:receiver-handoff, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:bridge-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: exclusive-owner-unverified}
  - {fact_id: fact:poller, subject_id: n:telegram-poller, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:poller-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: deployed-bytes-unverified}
  - {fact_id: fact:delivery, subject_id: n:mailbox, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:mailbox-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: end-effect-unverified}
  - {fact_id: fact:health, subject_id: n:health, field: mechanism.exists, plane: landed_mechanism, canonical_source_ref: null, non_authority_source_ref: na:health-code, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: frozen-revision, limitations: fleet-deployment-unverified}
  - {fact_id: fact:continuity, subject_id: n:continuity, field: declared.single-authority, plane: declared_contract, canonical_source_ref: a:org057, non_authority_source_ref: null, value_digest: {status: deferred, owner_ticket: T-2956, reason: source-bytes-not-canonicalized}, observed_at: null, freshness_rule: source-digest-exact, limitations: adoption-unverified}
  - {fact_id: fact:open-cutover, subject_id: trace:persona-bringup, field: open_work.cutover, plane: open_work, canonical_source_ref: null, non_authority_source_ref: na:t2953, value_digest: {status: deferred, owner_ticket: T-2956, reason: ticket-bytes-not-canonicalized}, observed_at: null, freshness_rule: ticket-revision, limitations: never-runtime-truth}

applicability:
  - {obligation_id: app:lifecycle.install, scope_ref: trace:persona-bringup, kind: lifecycle_leg, name: install, disposition: applicable, authority_ref: a:org068, reason: bringup-creates-owned-state, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:lifecycle.configure, scope_ref: trace:persona-bringup, kind: lifecycle_leg, name: configure, disposition: applicable, authority_ref: a:org068, reason: manifest-resolves-runtime, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:lifecycle.health, scope_ref: trace:persona-bringup, kind: lifecycle_leg, name: health, disposition: applicable, authority_ref: a:org068, reason: acceptance-requires-health, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:lifecycle.update, scope_ref: trace:persona-bringup, kind: lifecycle_leg, name: update, disposition: not_applicable, authority_ref: a:org068, reason: bringup-trace-not-update-trace, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:lifecycle.remove, scope_ref: trace:persona-bringup, kind: lifecycle_leg, name: remove, disposition: not_applicable, authority_ref: a:org068, reason: bringup-trace-uses-rollback-not-decommission, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:identity, scope_ref: trace:persona-bringup, kind: identity, name: persona-identity, disposition: applicable, authority_ref: a:org050, reason: actor-and-target-must-resolve, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:security, scope_ref: trace:persona-bringup, kind: security, name: capability-crossings, disposition: applicable, authority_ref: a:org050, reason: cross-uid-and-receiver-authority, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:credential, scope_ref: trace:persona-bringup, kind: credential, name: telegram-token-reference, disposition: applicable, authority_ref: a:org050, reason: inbound-and-outbound-telegram-boundaries-require-referenced-credentials, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:receiver, scope_ref: trace:persona-bringup, kind: receiver, name: exclusive-poller, disposition: applicable, authority_ref: a:org057, reason: exactly-one-inbound-owner, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:delivery, scope_ref: trace:persona-bringup, kind: delivery, name: end-to-end-effect, disposition: applicable, authority_ref: a:org084, reason: component-green-is-insufficient, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:continuity, scope_ref: trace:persona-bringup, kind: continuity, name: single-authority-handoff, disposition: applicable, authority_ref: a:org057, reason: preserve-persona-and-task, waiver_ref: null, expires_at_or_trigger: null}
  - {obligation_id: app:rollback, scope_ref: trace:persona-bringup, kind: rollback, name: owned-inverse, disposition: applicable, authority_ref: a:org050, reason: bringup-mutates-owned-state, waiver_ref: null, expires_at_or_trigger: null}

conditions:
  - {condition_id: c:manifest-valid, predicate: manifest-digest-and-schema-valid}
  - {condition_id: c:identity-attested, predicate: actor-target-and-account-resolve}
  - {condition_id: c:owned-intent-durable, predicate: inverse-intent-persisted-before-mutation}
  - {condition_id: c:runtime-ready, predicate: selected-runtime-input-ready}
  - {condition_id: c:old-receiver-absent, predicate: previous-inbound-owner-proven-absent}
  - {condition_id: c:receiver-exclusive, predicate: new-owner-present-and-no-sustained-409}
  - {condition_id: c:spooled, predicate: durable-spool-precedes-offset-advance}
  - {condition_id: c:wake-submitted, predicate: wake-injection-accepted}
  - {condition_id: c:turn-started, predicate: turn-start-correlated}
  - {condition_id: c:mailbox-consumed, predicate: correlated-mailbox-entry-consumed-by-started-turn}
  - {condition_id: c:handler-acked, predicate: correlated-handler-completion-receipt-durable}
  - {condition_id: c:effect-proven, predicate: external-effect-correlated-and-asserted}
  - {condition_id: c:health-current, predicate: health-evidence-fresh-for-source-revision}
  - {condition_id: c:rollback-reachable, predicate: every-mutating-failure-has-owned-inverse-or-compensation}

failures:
  - {failure_id: fail:invalid-input, class: invalid_input}
  - {failure_id: fail:identity, class: identity_mismatch}
  - {failure_id: fail:authority, class: authority_denied}
  - {failure_id: fail:drift, class: stale_drift}
  - {failure_id: fail:readiness, class: readiness_failed}
  - {failure_id: fail:split-brain, class: split_brain}
  - {failure_id: fail:delivery, class: delivery_incomplete}
  - {failure_id: fail:ack, class: acknowledgement_ambiguous}
  - {failure_id: fail:effect, class: effect_unproven}
  - {failure_id: fail:health, class: unavailable}
  - {failure_id: fail:rollback, class: rollback_failed}

receipts:
  - {receipt_id: receipt:manifest, schema_or_requirement: manifest-digest-validation}
  - {receipt_id: receipt:install, schema_or_requirement: pantheon-persona-install-receipt/v1}
  - {receipt_id: receipt:rollback, schema_or_requirement: owned-preimage-and-inverse-result}
  - {receipt_id: receipt:viability, schema_or_requirement: pantheon-runtime-initial-viability/v1}
  - {receipt_id: receipt:handoff, schema_or_requirement: old-absent-new-present-no409}
  - {receipt_id: receipt:spool, schema_or_requirement: durable-file-before-offset}
  - {receipt_id: receipt:mailbox-ack, schema_or_requirement: handler-completed-only}
  - {receipt_id: receipt:wake, schema_or_requirement: signal-submitted-only}
  - {receipt_id: receipt:turn, schema_or_requirement: turn-started}
  - {receipt_id: receipt:effect, schema_or_requirement: asserted-effect-proven}
  - {receipt_id: receipt:health, schema_or_requirement: pantheon-health/v1-with-freshness}
  - {receipt_id: receipt:continuity, schema_or_requirement: single-authority-transfer}

boundaries:
  - {boundary_id: boundary:identity, from_domain: caller, to_domain: target-account, capability: attested-identity}
  - {boundary_id: boundary:owned-state, from_domain: planner, to_domain: host-state, capability: typed-owned-mutation}
  - {boundary_id: boundary:runtime, from_domain: controller, to_domain: harness, capability: launch-and-input}
  - {boundary_id: boundary:receiver, from_domain: old-owner, to_domain: new-poller, capability: getUpdates-ownership}
  - {boundary_id: boundary:telegram, from_domain: telegram, to_domain: durable-spool, capability: inbound-delivery}
  - {boundary_id: boundary:mailbox, from_domain: spool, to_domain: harness-turn, capability: at-least-once-delivery}
  - {boundary_id: boundary:health, from_domain: component, to_domain: health-bus, capability: read-only-observation}
  - {boundary_id: boundary:continuity, from_domain: active-runtime, to_domain: successor-or-rollback, capability: single-authority-transfer}

rollbacks:
  - {rollback_id: rb:nonmutating, disposition: non_mutating, failure_state_refs: [], inverse_edge_refs: [], owner: orchestrator, authority_ref: a:org084, receipt_requirement: none}
  - {rollback_id: rb:owned-inverse, disposition: reversible_inverse, failure_state_refs: [fail:rollback], inverse_edge_refs: [e:owned-state-enables-rollback], owner: recovery, authority_ref: a:org050, receipt_requirement: receipt:rollback}
  - {rollback_id: rb:refuse, disposition: refused_before_mutation, failure_state_refs: [fail:invalid-input, fail:identity, fail:authority, fail:drift], inverse_edge_refs: [], owner: orchestrator, authority_ref: a:org050, receipt_requirement: receipt:manifest}
  - {rollback_id: rb:receiver-restore, disposition: compensating_recovery, failure_state_refs: [fail:readiness, fail:split-brain], inverse_edge_refs: [e:continuity-invokes-rollback], owner: recovery, authority_ref: a:org057, receipt_requirement: receipt:handoff}
  - {rollback_id: rb:runtime-teardown, disposition: compensating_recovery, failure_state_refs: [fail:readiness], inverse_edge_refs: [e:continuity-invokes-rollback], owner: orchestrator, authority_ref: a:org080, receipt_requirement: receipt:viability}

nodes:
  - {node_id: n:manifest, layer: 1, kind: desired-manifest, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:manifest, fact_refs: [fact:manifest], applicability_refs: [app:lifecycle.configure, app:identity]}
  - {node_id: n:identity, layer: 1, kind: persona-identity, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:identity, fact_refs: [fact:identity], applicability_refs: [app:identity, app:security]}
  - {node_id: n:installer, layer: 2, kind: lifecycle-executor, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:installer, fact_refs: [fact:installer], applicability_refs: [app:lifecycle.install, app:security, app:rollback]}
  - {node_id: n:owned-state, layer: 2, kind: owned-state-ledger, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:owned-state, fact_refs: [fact:installer], applicability_refs: [app:lifecycle.install, app:rollback]}
  - {node_id: n:runtime-controller, layer: 3, kind: ordered-viability-controller, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:runtime-controller, fact_refs: [fact:runtime], applicability_refs: [app:lifecycle.configure, app:lifecycle.health, app:continuity]}
  - {node_id: n:harness-adapter, layer: 3, kind: harness-adapter, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:harness-adapter, fact_refs: [fact:runtime], applicability_refs: [app:lifecycle.configure, app:security]}
  - {node_id: n:receiver-handoff, layer: 4, kind: exclusive-owner-transfer, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:receiver-handoff, fact_refs: [fact:receiver], applicability_refs: [app:receiver, app:continuity, app:rollback]}
  - {node_id: n:telegram-poller, layer: 4, kind: inbound-poller, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:telegram-poller, fact_refs: [fact:poller], applicability_refs: [app:receiver, app:security, app:credential, app:delivery]}
  - {node_id: n:durable-spool, layer: 5, kind: durable-spool, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:durable-spool, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {node_id: n:mailbox, layer: 5, kind: at-least-once-mailbox, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:mailbox, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {node_id: n:wake, layer: 5, kind: best-effort-wake, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:wake, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {node_id: n:harness-turn, layer: 5, kind: agent-turn, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:harness-turn, fact_refs: [fact:delivery], applicability_refs: [app:delivery, app:continuity]}
  - {node_id: n:external-effect, layer: 5, kind: asserted-external-effect, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:external-effect, fact_refs: [fact:delivery], applicability_refs: [app:credential, app:delivery]}
  - {node_id: n:health, layer: 6, kind: health-dbus, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:health, fact_refs: [fact:health], applicability_refs: [app:lifecycle.health, app:security]}
  - {node_id: n:continuity, layer: 6, kind: continuity-controller, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:continuity, fact_refs: [fact:continuity, fact:open-cutover], applicability_refs: [app:continuity, app:receiver, app:rollback]}
  - {node_id: n:rollback, layer: 6, kind: rollback-controller, atlas_identity_mapping_ref: map:runtime-profile, contract_ref: oc:n:rollback, fact_refs: [fact:installer, fact:continuity], applicability_refs: [app:rollback, app:security]}

edges:
  - {edge_id: e:manifest-identifies-persona, source_node_id: n:manifest, target_node_id: n:identity, relation: identifies, relation_class: requires, contract_ref: oc:e:manifest-identifies-persona, fact_refs: [fact:manifest, fact:identity], applicability_refs: [app:identity]}
  - {edge_id: e:identity-secures-installer, source_node_id: n:identity, target_node_id: n:installer, relation: authorizes, relation_class: secures, contract_ref: oc:e:identity-secures-installer, fact_refs: [fact:identity, fact:installer], applicability_refs: [app:identity, app:security]}
  - {edge_id: e:manifest-authorizes-installer, source_node_id: n:manifest, target_node_id: n:installer, relation: configures, relation_class: requires, contract_ref: oc:e:manifest-authorizes-installer, fact_refs: [fact:manifest, fact:installer], applicability_refs: [app:lifecycle.configure, app:security]}
  - {edge_id: e:installer-mutates-owned-state, source_node_id: n:installer, target_node_id: n:owned-state, relation: writes-owned, relation_class: produces, contract_ref: oc:e:installer-mutates-owned-state, fact_refs: [fact:installer], applicability_refs: [app:lifecycle.install, app:rollback]}
  - {edge_id: e:owned-state-enables-rollback, source_node_id: n:owned-state, target_node_id: n:rollback, relation: inverse, relation_class: rollback, contract_ref: oc:e:owned-state-enables-rollback, fact_refs: [fact:installer], applicability_refs: [app:rollback]}
  - {edge_id: e:manifest-orders-runtime, source_node_id: n:manifest, target_node_id: n:runtime-controller, relation: candidate-order, relation_class: order, contract_ref: oc:e:manifest-orders-runtime, fact_refs: [fact:manifest, fact:runtime], applicability_refs: [app:lifecycle.configure]}
  - {edge_id: e:identity-secures-controller, source_node_id: n:identity, target_node_id: n:runtime-controller, relation: actor-scope, relation_class: secures, contract_ref: oc:e:identity-secures-controller, fact_refs: [fact:identity, fact:runtime], applicability_refs: [app:identity, app:security]}
  - {edge_id: e:controller-launches-adapter, source_node_id: n:runtime-controller, target_node_id: n:harness-adapter, relation: launch, relation_class: produces, contract_ref: oc:e:controller-launches-adapter, fact_refs: [fact:runtime], applicability_refs: [app:lifecycle.configure, app:security]}
  - {edge_id: e:adapter-proves-readiness, source_node_id: n:harness-adapter, target_node_id: n:runtime-controller, relation: readiness, relation_class: evidenced_by, contract_ref: oc:e:adapter-proves-readiness, fact_refs: [fact:runtime], applicability_refs: [app:lifecycle.configure]}
  - {edge_id: e:controller-initiates-handoff, source_node_id: n:runtime-controller, target_node_id: n:receiver-handoff, relation: receiver-transfer, relation_class: hands_off, contract_ref: oc:e:controller-initiates-handoff, fact_refs: [fact:runtime, fact:receiver], applicability_refs: [app:receiver, app:continuity]}
  - {edge_id: e:identity-secures-receiver, source_node_id: n:identity, target_node_id: n:receiver-handoff, relation: receiver-authority, relation_class: secures, contract_ref: oc:e:identity-secures-receiver, fact_refs: [fact:identity, fact:receiver], applicability_refs: [app:identity, app:security, app:credential, app:receiver]}
  - {edge_id: e:handoff-starts-poller, source_node_id: n:receiver-handoff, target_node_id: n:telegram-poller, relation: exclusive-owner, relation_class: hands_off, contract_ref: oc:e:handoff-starts-poller, fact_refs: [fact:receiver, fact:poller], applicability_refs: [app:receiver, app:credential]}
  - {edge_id: e:telegram-durably-spools, source_node_id: n:telegram-poller, target_node_id: n:durable-spool, relation: fetched-to-durable, relation_class: produces, contract_ref: oc:e:telegram-durably-spools, fact_refs: [fact:poller, fact:delivery], applicability_refs: [app:receiver, app:credential, app:delivery]}
  - {edge_id: e:spool-routes-mailbox, source_node_id: n:durable-spool, target_node_id: n:mailbox, relation: route, relation_class: transports, contract_ref: oc:e:spool-routes-mailbox, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {edge_id: e:mailbox-signals-wake, source_node_id: n:mailbox, target_node_id: n:wake, relation: signal, relation_class: produces, contract_ref: oc:e:mailbox-signals-wake, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {edge_id: e:wake-submits-turn, source_node_id: n:wake, target_node_id: n:harness-turn, relation: wake-to-turn, relation_class: transports, contract_ref: oc:e:wake-submits-turn, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {edge_id: e:turn-consumes-mailbox, source_node_id: n:mailbox, target_node_id: n:harness-turn, relation: payload-consumption, relation_class: consumes, contract_ref: oc:e:turn-consumes-mailbox, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {edge_id: e:turn-acks-handler, source_node_id: n:harness-turn, target_node_id: n:mailbox, relation: handler-complete, relation_class: evidenced_by, contract_ref: oc:e:turn-acks-handler, fact_refs: [fact:delivery], applicability_refs: [app:delivery]}
  - {edge_id: e:turn-proves-effect, source_node_id: n:harness-turn, target_node_id: n:external-effect, relation: effect, relation_class: evidenced_by, contract_ref: oc:e:turn-proves-effect, fact_refs: [fact:delivery], applicability_refs: [app:credential, app:delivery]}
  - {edge_id: e:controller-emits-health, source_node_id: n:runtime-controller, target_node_id: n:health, relation: health, relation_class: observes, contract_ref: oc:e:controller-emits-health, fact_refs: [fact:runtime, fact:health], applicability_refs: [app:lifecycle.health]}
  - {edge_id: e:health-triggers-continuity, source_node_id: n:health, target_node_id: n:continuity, relation: degraded, relation_class: degrades, contract_ref: oc:e:health-triggers-continuity, fact_refs: [fact:health, fact:continuity], applicability_refs: [app:lifecycle.health, app:continuity]}
  - {edge_id: e:continuity-transfers-authority, source_node_id: n:continuity, target_node_id: n:runtime-controller, relation: single-authority, relation_class: continuity_transfer, contract_ref: oc:e:continuity-transfers-authority, fact_refs: [fact:continuity, fact:runtime], applicability_refs: [app:continuity, app:receiver]}
  - {edge_id: e:continuity-invokes-rollback, source_node_id: n:continuity, target_node_id: n:rollback, relation: recover, relation_class: rollback, contract_ref: oc:e:continuity-invokes-rollback, fact_refs: [fact:continuity, fact:installer], applicability_refs: [app:continuity, app:rollback]}

object_contracts:
  - {contract_id: oc:n:manifest, subject_id: n:manifest, authoritative_input_refs: [a:manifest], steward_ref: orchestrator, runtime_actor_ref: manifest-validator, precondition_refs: [c:manifest-valid], postcondition_refs: [c:identity-attested], failure_refs: [fail:invalid-input, fail:drift], receipt_refs: [receipt:manifest], boundary_refs: [boundary:identity], rollback_ref: rb:refuse}
  - {contract_id: oc:n:identity, subject_id: n:identity, authoritative_input_refs: [a:org050, a:manifest], steward_ref: orchestrator, runtime_actor_ref: identity-resolver, precondition_refs: [c:manifest-valid], postcondition_refs: [c:identity-attested], failure_refs: [fail:identity, fail:authority], receipt_refs: [receipt:manifest], boundary_refs: [boundary:identity], rollback_ref: rb:refuse}
  - {contract_id: oc:n:installer, subject_id: n:installer, authoritative_input_refs: [a:org050, a:org079, a:org080], steward_ref: orchestrator, runtime_actor_ref: persona-installer, precondition_refs: [c:identity-attested, c:owned-intent-durable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:authority, fail:rollback], receipt_refs: [receipt:install], boundary_refs: [boundary:identity, boundary:owned-state], rollback_ref: rb:owned-inverse}
  - {contract_id: oc:n:owned-state, subject_id: n:owned-state, authoritative_input_refs: [a:org050, a:org080], steward_ref: orchestrator, runtime_actor_ref: receipt-store, precondition_refs: [c:owned-intent-durable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:drift, fail:rollback], receipt_refs: [receipt:install, receipt:rollback], boundary_refs: [boundary:owned-state], rollback_ref: rb:owned-inverse}
  - {contract_id: oc:n:runtime-controller, subject_id: n:runtime-controller, authoritative_input_refs: [a:manifest, a:org080], steward_ref: orchestrator, runtime_actor_ref: runtime-controller, precondition_refs: [c:manifest-valid, c:identity-attested], postcondition_refs: [c:runtime-ready], failure_refs: [fail:readiness, fail:drift], receipt_refs: [receipt:viability], boundary_refs: [boundary:runtime], rollback_ref: rb:runtime-teardown}
  - {contract_id: oc:n:harness-adapter, subject_id: n:harness-adapter, authoritative_input_refs: [a:org080], steward_ref: orchestrator, runtime_actor_ref: harness-adapter, precondition_refs: [c:identity-attested], postcondition_refs: [c:runtime-ready], failure_refs: [fail:readiness, fail:authority], receipt_refs: [receipt:viability], boundary_refs: [boundary:identity, boundary:runtime], rollback_ref: rb:runtime-teardown}
  - {contract_id: oc:n:receiver-handoff, subject_id: n:receiver-handoff, authoritative_input_refs: [a:org050, a:org057], steward_ref: orchestrator, runtime_actor_ref: receiver-handoff-adapter, precondition_refs: [c:runtime-ready, c:old-receiver-absent], postcondition_refs: [c:receiver-exclusive], failure_refs: [fail:split-brain, fail:readiness], receipt_refs: [receipt:handoff], boundary_refs: [boundary:identity, boundary:receiver], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:n:telegram-poller, subject_id: n:telegram-poller, authoritative_input_refs: [a:manifest, a:org050], steward_ref: orchestrator, runtime_actor_ref: codex-poller, precondition_refs: [c:receiver-exclusive], postcondition_refs: [c:spooled], failure_refs: [fail:split-brain, fail:delivery], receipt_refs: [receipt:handoff, receipt:spool], boundary_refs: [boundary:receiver, boundary:telegram], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:n:durable-spool, subject_id: n:durable-spool, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: codex-poller, precondition_refs: [c:receiver-exclusive], postcondition_refs: [c:spooled], failure_refs: [fail:delivery], receipt_refs: [receipt:spool], boundary_refs: [boundary:telegram], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:mailbox, subject_id: n:mailbox, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-consumer, precondition_refs: [c:spooled], postcondition_refs: [c:turn-started], failure_refs: [fail:delivery, fail:ack], receipt_refs: [receipt:mailbox-ack], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:wake, subject_id: n:wake, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-wake, precondition_refs: [c:spooled], postcondition_refs: [c:wake-submitted], failure_refs: [fail:delivery], receipt_refs: [receipt:wake], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:harness-turn, subject_id: n:harness-turn, authoritative_input_refs: [a:org057, a:org084], steward_ref: orchestrator, runtime_actor_ref: active-harness, precondition_refs: [c:wake-submitted], postcondition_refs: [c:turn-started], failure_refs: [fail:delivery, fail:ack], receipt_refs: [receipt:turn, receipt:mailbox-ack], boundary_refs: [boundary:mailbox, boundary:continuity], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:external-effect, subject_id: n:external-effect, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: active-harness, precondition_refs: [c:handler-acked], postcondition_refs: [c:effect-proven], failure_refs: [fail:effect], receipt_refs: [receipt:effect], boundary_refs: [boundary:telegram], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:health, subject_id: n:health, authoritative_input_refs: [a:org052, a:org068], steward_ref: recovery, runtime_actor_ref: health-collector, precondition_refs: [c:runtime-ready], postcondition_refs: [c:health-current], failure_refs: [fail:health, fail:drift], receipt_refs: [receipt:health], boundary_refs: [boundary:health], rollback_ref: rb:nonmutating}
  - {contract_id: oc:n:continuity, subject_id: n:continuity, authoritative_input_refs: [a:org057, a:org068], steward_ref: recovery, runtime_actor_ref: continuity-controller, precondition_refs: [c:health-current], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:split-brain, fail:health], receipt_refs: [receipt:continuity], boundary_refs: [boundary:continuity], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:n:rollback, subject_id: n:rollback, authoritative_input_refs: [a:org050, a:org057, a:org080], steward_ref: recovery, runtime_actor_ref: rollback-controller, precondition_refs: [c:owned-intent-durable, c:rollback-reachable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:rollback], receipt_refs: [receipt:rollback, receipt:continuity], boundary_refs: [boundary:owned-state, boundary:continuity], rollback_ref: rb:owned-inverse}

  - {contract_id: oc:e:manifest-identifies-persona, subject_id: e:manifest-identifies-persona, authoritative_input_refs: [a:manifest, a:org050], steward_ref: orchestrator, runtime_actor_ref: identity-resolver, precondition_refs: [c:manifest-valid], postcondition_refs: [c:identity-attested], failure_refs: [fail:identity, fail:drift], receipt_refs: [receipt:manifest], boundary_refs: [boundary:identity], rollback_ref: rb:refuse}
  - {contract_id: oc:e:identity-secures-installer, subject_id: e:identity-secures-installer, authoritative_input_refs: [a:org050], steward_ref: orchestrator, runtime_actor_ref: persona-installer, precondition_refs: [c:identity-attested], postcondition_refs: [c:owned-intent-durable], failure_refs: [fail:identity, fail:authority], receipt_refs: [receipt:manifest], boundary_refs: [boundary:identity, boundary:owned-state], rollback_ref: rb:refuse}
  - {contract_id: oc:e:manifest-authorizes-installer, subject_id: e:manifest-authorizes-installer, authoritative_input_refs: [a:manifest, a:org079], steward_ref: orchestrator, runtime_actor_ref: manifest-validator, precondition_refs: [c:manifest-valid, c:identity-attested], postcondition_refs: [c:owned-intent-durable], failure_refs: [fail:invalid-input, fail:authority], receipt_refs: [receipt:manifest], boundary_refs: [boundary:identity, boundary:owned-state], rollback_ref: rb:refuse}
  - {contract_id: oc:e:installer-mutates-owned-state, subject_id: e:installer-mutates-owned-state, authoritative_input_refs: [a:org050, a:org080], steward_ref: orchestrator, runtime_actor_ref: persona-installer, precondition_refs: [c:owned-intent-durable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:authority, fail:rollback], receipt_refs: [receipt:install], boundary_refs: [boundary:owned-state], rollback_ref: rb:owned-inverse}
  - {contract_id: oc:e:owned-state-enables-rollback, subject_id: e:owned-state-enables-rollback, authoritative_input_refs: [a:org050, a:org080], steward_ref: recovery, runtime_actor_ref: rollback-controller, precondition_refs: [c:owned-intent-durable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:rollback], receipt_refs: [receipt:rollback], boundary_refs: [boundary:owned-state], rollback_ref: rb:owned-inverse}
  - {contract_id: oc:e:manifest-orders-runtime, subject_id: e:manifest-orders-runtime, authoritative_input_refs: [a:manifest, a:org080], steward_ref: orchestrator, runtime_actor_ref: runtime-controller, precondition_refs: [c:manifest-valid], postcondition_refs: [c:runtime-ready], failure_refs: [fail:invalid-input, fail:readiness], receipt_refs: [receipt:viability], boundary_refs: [boundary:runtime], rollback_ref: rb:runtime-teardown}
  - {contract_id: oc:e:identity-secures-controller, subject_id: e:identity-secures-controller, authoritative_input_refs: [a:org050], steward_ref: orchestrator, runtime_actor_ref: runtime-controller, precondition_refs: [c:identity-attested], postcondition_refs: [c:runtime-ready], failure_refs: [fail:identity, fail:authority], receipt_refs: [receipt:viability], boundary_refs: [boundary:identity, boundary:runtime], rollback_ref: rb:refuse}
  - {contract_id: oc:e:controller-launches-adapter, subject_id: e:controller-launches-adapter, authoritative_input_refs: [a:org080], steward_ref: orchestrator, runtime_actor_ref: runtime-controller, precondition_refs: [c:identity-attested], postcondition_refs: [c:runtime-ready], failure_refs: [fail:readiness], receipt_refs: [receipt:viability], boundary_refs: [boundary:runtime], rollback_ref: rb:runtime-teardown}
  - {contract_id: oc:e:adapter-proves-readiness, subject_id: e:adapter-proves-readiness, authoritative_input_refs: [a:org080], steward_ref: orchestrator, runtime_actor_ref: harness-adapter, precondition_refs: [c:identity-attested], postcondition_refs: [c:runtime-ready], failure_refs: [fail:readiness, fail:ack], receipt_refs: [receipt:viability], boundary_refs: [boundary:runtime], rollback_ref: rb:runtime-teardown}
  - {contract_id: oc:e:controller-initiates-handoff, subject_id: e:controller-initiates-handoff, authoritative_input_refs: [a:org057, a:org080], steward_ref: orchestrator, runtime_actor_ref: runtime-controller, precondition_refs: [c:runtime-ready, c:old-receiver-absent], postcondition_refs: [c:receiver-exclusive], failure_refs: [fail:readiness, fail:split-brain], receipt_refs: [receipt:viability, receipt:handoff], boundary_refs: [boundary:runtime, boundary:receiver], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:e:identity-secures-receiver, subject_id: e:identity-secures-receiver, authoritative_input_refs: [a:org050, a:org057], steward_ref: orchestrator, runtime_actor_ref: receiver-handoff-adapter, precondition_refs: [c:identity-attested], postcondition_refs: [c:old-receiver-absent], failure_refs: [fail:identity, fail:authority, fail:split-brain], receipt_refs: [receipt:handoff], boundary_refs: [boundary:identity, boundary:receiver], rollback_ref: rb:refuse}
  - {contract_id: oc:e:handoff-starts-poller, subject_id: e:handoff-starts-poller, authoritative_input_refs: [a:manifest, a:org057], steward_ref: orchestrator, runtime_actor_ref: receiver-handoff-adapter, precondition_refs: [c:old-receiver-absent], postcondition_refs: [c:receiver-exclusive], failure_refs: [fail:split-brain], receipt_refs: [receipt:handoff], boundary_refs: [boundary:receiver], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:e:telegram-durably-spools, subject_id: e:telegram-durably-spools, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: codex-poller, precondition_refs: [c:receiver-exclusive], postcondition_refs: [c:spooled], failure_refs: [fail:delivery, fail:split-brain], receipt_refs: [receipt:spool], boundary_refs: [boundary:receiver, boundary:telegram], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:e:spool-routes-mailbox, subject_id: e:spool-routes-mailbox, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-router, precondition_refs: [c:spooled], postcondition_refs: [c:spooled], failure_refs: [fail:delivery], receipt_refs: [receipt:spool], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:mailbox-signals-wake, subject_id: e:mailbox-signals-wake, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-wake, precondition_refs: [c:spooled], postcondition_refs: [c:wake-submitted], failure_refs: [fail:delivery], receipt_refs: [receipt:wake], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:wake-submits-turn, subject_id: e:wake-submits-turn, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-wake, precondition_refs: [c:wake-submitted], postcondition_refs: [c:turn-started], failure_refs: [fail:delivery, fail:ack], receipt_refs: [receipt:wake, receipt:turn], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:turn-consumes-mailbox, subject_id: e:turn-consumes-mailbox, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-consumer, precondition_refs: [c:spooled, c:turn-started], postcondition_refs: [c:mailbox-consumed], failure_refs: [fail:delivery, fail:ack], receipt_refs: [receipt:mailbox-ack, receipt:turn], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:turn-acks-handler, subject_id: e:turn-acks-handler, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: mailbox-consumer, precondition_refs: [c:mailbox-consumed], postcondition_refs: [c:handler-acked], failure_refs: [fail:ack], receipt_refs: [receipt:mailbox-ack], boundary_refs: [boundary:mailbox], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:turn-proves-effect, subject_id: e:turn-proves-effect, authoritative_input_refs: [a:org084], steward_ref: orchestrator, runtime_actor_ref: active-harness, precondition_refs: [c:handler-acked], postcondition_refs: [c:effect-proven], failure_refs: [fail:effect], receipt_refs: [receipt:effect], boundary_refs: [boundary:telegram], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:controller-emits-health, subject_id: e:controller-emits-health, authoritative_input_refs: [a:org052, a:org068], steward_ref: recovery, runtime_actor_ref: runtime-controller, precondition_refs: [c:runtime-ready], postcondition_refs: [c:health-current], failure_refs: [fail:health, fail:drift], receipt_refs: [receipt:health], boundary_refs: [boundary:health], rollback_ref: rb:nonmutating}
  - {contract_id: oc:e:health-triggers-continuity, subject_id: e:health-triggers-continuity, authoritative_input_refs: [a:org057, a:org068], steward_ref: recovery, runtime_actor_ref: continuity-controller, precondition_refs: [c:health-current], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:health, fail:split-brain], receipt_refs: [receipt:health, receipt:continuity], boundary_refs: [boundary:health, boundary:continuity], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:e:continuity-transfers-authority, subject_id: e:continuity-transfers-authority, authoritative_input_refs: [a:org057], steward_ref: recovery, runtime_actor_ref: continuity-controller, precondition_refs: [c:health-current, c:old-receiver-absent], postcondition_refs: [c:runtime-ready, c:receiver-exclusive], failure_refs: [fail:split-brain, fail:readiness], receipt_refs: [receipt:continuity, receipt:handoff], boundary_refs: [boundary:receiver, boundary:continuity], rollback_ref: rb:receiver-restore}
  - {contract_id: oc:e:continuity-invokes-rollback, subject_id: e:continuity-invokes-rollback, authoritative_input_refs: [a:org050, a:org057, a:org080], steward_ref: recovery, runtime_actor_ref: rollback-controller, precondition_refs: [c:owned-intent-durable, c:rollback-reachable], postcondition_refs: [c:rollback-reachable], failure_refs: [fail:rollback], receipt_refs: [receipt:rollback, receipt:continuity], boundary_refs: [boundary:owned-state, boundary:continuity], rollback_ref: rb:owned-inverse}

traces:
  - trace_id: trace:persona-bringup
    trace_kind: persona-bringup
    graph_digest: {status: deferred, owner_ticket: T-2956, reason: graph-validator-not-built}
    schema_digest: {status: deferred, owner_ticket: T-2956, reason: schema-artifact-not-built}
    source_refs: [fs:pantheon, fs:codex, fs:org084-candidate]
    start_state: desired-persona
    end_state: asserted-external-effect-and-current-health
    initial_condition_refs: [c:manifest-valid]
    ordered_edge_ids: [e:manifest-identifies-persona, e:identity-secures-installer, e:manifest-authorizes-installer, e:installer-mutates-owned-state, e:manifest-orders-runtime, e:identity-secures-controller, e:controller-launches-adapter, e:adapter-proves-readiness, e:identity-secures-receiver, e:controller-initiates-handoff, e:handoff-starts-poller, e:telegram-durably-spools, e:spool-routes-mailbox, e:mailbox-signals-wake, e:wake-submits-turn, e:turn-consumes-mailbox, e:turn-acks-handler, e:turn-proves-effect, e:controller-emits-health]
    applicability_refs: [app:lifecycle.install, app:lifecycle.configure, app:lifecycle.health, app:lifecycle.update, app:lifecycle.remove, app:identity, app:security, app:credential, app:receiver, app:delivery, app:continuity, app:rollback]
    required_delivery_milestones: [fetched, durably_queued, submitted, turn_started, consumed_acked, externally_responded, asserted_effect_proven]
    failure_exits:
      - {failure_refs: [fail:invalid-input, fail:identity, fail:authority, fail:drift], rollback_ref: rb:refuse}
      - {failure_refs: [fail:readiness], rollback_ref: rb:runtime-teardown}
      - {failure_refs: [fail:split-brain], rollback_ref: rb:receiver-restore}
      - {failure_refs: [fail:rollback], rollback_ref: rb:owned-inverse}
    rollback_edge_ids: [e:owned-state-enables-rollback, e:continuity-invokes-rollback]
    limitations: inventory-example-not-proof-of-deployment

architectural_exercise_refs:
  - {exercise_id: exercise:deferred-org083, subject_refs: [trace:persona-bringup], evidence_contract_ref: na:org083, producer: null, transport: null, consumer: null, reached_preconditions: [], asserted_effect: null, immutable_revision: null, artifact_digest: {status: deferred, owner_ticket: T-3380, reason: org083-evidence-machinery-not-built}, limitations: ORG-083-is-proposed-and-unbuilt, status: deferred}

operator_sheets:
  - {sheet_id: sheet:persona-bringup-example, trace_ref: trace:persona-bringup, request_digest: {status: deferred, owner_ticket: T-2956, reason: no-generation-request}, schema_digest: {status: deferred, owner_ticket: T-2956, reason: schema-artifact-not-built}, graph_digest: {status: deferred, owner_ticket: T-2956, reason: graph-validator-not-built}, source_refs: [fs:pantheon, fs:codex, fs:org084-candidate], evidence_digest: {status: deferred, owner_ticket: T-3380, reason: org083-evidence-machinery-not-built}, generated_at: null, freshness_rule: regenerate-after-any-source-change, authority: projection, commands_authoritative: false, status: not-generated}

migration:
  - {row_id: mig:org049, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-049-manifest-driven-installation-and-convergent-bringup.md#Decision-proposal", presence: AVAILABLE, claim: historical-layer-and-mode-taxonomy, evidence_class: superseded-contract, authority_lifecycle: superseded-by-ORG-080, implementation: built-historical, installed_adopted: not_applicable, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-2667, disposition: AVAILABLE, limitation: not-current-composer-authority, falsifier: cited-as-current-authority}
  - {row_id: mig:org050, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-050-security-and-identity-contract.md#Decision", presence: AVAILABLE, claim: identity-trust-and-owned-rollback-contract, evidence_class: ratified-contract, authority_lifecycle: ratified, implementation: built, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3277, disposition: UNVERIFIED, limitation: no-live-identity-probe, falsifier: implicit-trust-or-unowned-deletion}
  - {row_id: mig:org052, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-052-harness-neutral-host-capability-broker.md#4-the-public-contract-is-closed-handle-based-and-one-to-one", presence: AVAILABLE, claim: privilege-free-adapter-and-system-bus-boundary, evidence_class: ratified-amended-contract, authority_lifecycle: ratified, implementation: partial-scaffold, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652, disposition: DEFERRED, limitation: no-production-actuation-claim, falsifier: adapter-privilege-or-mailbox-authority}
  - {row_id: mig:org056, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-056-whole-organism-topology-and-organ-contracts.md#4-every-organ-contract-declares-failure-and-degradation", presence: AVAILABLE, claim: topology-ownership-and-recovery-fields, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2416, disposition: DEFERRED, limitation: not-runtime-proof, falsifier: unowned-or-unrecoverable-organ}
  - {row_id: mig:org057, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-057-continuity-and-harness-independent-selfhood.md#3-make-harness-switching-a-reversible-binding-change", presence: AVAILABLE, claim: exactly-one-authority-and-handoff-contract, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3175/T-3177, disposition: DEFERRED, limitation: no-fleet-adoption, falsifier: dual-live-authority}
  - {row_id: mig:org063, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-063-harness-manager-action-broker.md#Intent", presence: AVAILABLE, claim: uid-harness-cordon-and-isolates, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652, disposition: DEFERRED, limitation: not-live-boundary-proof, falsifier: harness-obtains-os-authority}
  - {row_id: mig:org064, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-064-general-scheduler-module.md#Intent", presence: AVAILABLE, claim: scheduler-isolate-contract, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2662, disposition: DEFERRED, limitation: scheduling-outside-initial-trace, falsifier: scheduler-owns-action}
  - {row_id: mig:org068, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-068-universal-system-lifecycle-contract.md#Decision", presence: AVAILABLE, claim: five-lifecycle-leg-obligations, evidence_class: ratified-amended-contract, authority_lifecycle: ratified-needs-build, implementation: registry-deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2667, disposition: DEFERRED, limitation: applicability-must-be-explicit, falsifier: omitted-applicable-leg}
  - {row_id: mig:org076, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-076-fleet-health-and-repair-system.md#Design", presence: AVAILABLE, claim: health-design-gap, evidence_class: rebounded-design, authority_lifecycle: do-not-ratify, implementation: no-authority-claim, installed_adopted: not_applicable, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-2944, disposition: UNVERIFIED, limitation: gap-reference-only, falsifier: cited-as-ratified-authority}
  - {row_id: mig:org077, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-077-health-plane.md#root", presence: AVAILABLE, claim: folded-pointer-only, evidence_class: superseded-pointer, authority_lifecycle: folded-into-ORG-076, implementation: not_applicable, installed_adopted: not_applicable, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-2944, disposition: AVAILABLE, limitation: no-standalone-content, falsifier: cited-as-independent-authority}
  - {row_id: mig:org079, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-079-l1-install-completeness-contract.md#Decision", presence: AVAILABLE, claim: l1-plan-apply-verify-completeness, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: partial, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3277, disposition: DEFERRED, limitation: code-is-partial, falsifier: absent-postcondition-treated-achieved}
  - {row_id: mig:org080, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-080-germination-engine.md#2-component-contract", presence: AVAILABLE, claim: sole-component-dag-receipt-and-rollback-authority, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: partial-planner-sandbox, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2667, disposition: DEFERRED, limitation: no-germinate-apex, falsifier: projection-redeclares-authoritative-field}
  - {row_id: mig:org081, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-081-system-registry.md#Decision", presence: AVAILABLE, claim: adoption-and-coverage-authority, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: registry-absent, installed_adopted: absent, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-2667, disposition: DEFERRED, limitation: no-runtime-truth, falsifier: registry-owns-component-or-runtime-field}
  - {row_id: mig:org082, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-082-broker-daemon-exposed-surface-amendment.md#Decision", presence: AVAILABLE, claim: substrate-off-daemon-and-same-digest-gate, evidence_class: ratified-contract, authority_lifecycle: ratified-needs-build, implementation: deferred, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652, disposition: DEFERRED, limitation: amendment-is-not-live-proof, falsifier: substrate-daemon-op-or-stale-digest}
  - {row_id: mig:org083, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:adr/ORG-083-code-coverage-governance-and-test-effectiveness-evidence.md#4-boundaries-effectiveness-and-efficiency", presence: AVAILABLE, claim: reachability-versus-effect-evidence-contract, evidence_class: proposed-design, authority_lifecycle: proposed-unratified, implementation: absent, installed_adopted: absent, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-3379/T-3380, disposition: DEFERRED, limitation: foreign-seam-only, falsifier: line-or-branch-coverage-proves-edge-effect}
  - {row_id: mig:atlas, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/architecture_atlas.py#SourcePin", presence: AVAILABLE, claim: derived-navigation-contract-exists, evidence_class: landed_mechanism, authority_lifecycle: non-authoritative-derived-view, implementation: landed, installed_adopted: not_applicable, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-3245, disposition: AVAILABLE, limitation: no-runtime-trace-semantics, falsifier: incompatible-source-pin-or-write-back}
  - {row_id: mig:atlas-schema, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:schemas/architecture-atlas-view.schema.json#root", presence: AVAILABLE, claim: atlas-view-serialization-exists, evidence_class: landed-schema, authority_lifecycle: non-authoritative-derived-view, implementation: landed, installed_adopted: not_applicable, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-3245, disposition: AVAILABLE, limitation: not-runtime-trace-schema, falsifier: runtime-trace-authority-inserted}
  - {row_id: mig:registry, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:registry/systems.yaml#root", presence: ABSENT, claim: ORG-081-registry-not-present, evidence_class: source-absence, authority_lifecycle: ratified-needs-build, implementation: absent, installed_adopted: absent, runtime_observation: not_applicable, freshness: frozen-revision, dependency: T-2667, disposition: DEFERRED, limitation: no-external-search, falsifier: file-exists-at-frozen-revision}
  - {row_id: mig:germinate, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/germinate/components/#root", presence: ABSENT, claim: apex-component-modules-not-present, evidence_class: source-absence, authority_lifecycle: ratified-needs-build, implementation: partial-planner-sandbox-only, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2667, disposition: DEFERRED, limitation: sandbox-is-not-apex, falsifier: module-exists-at-frozen-revision}
  - {row_id: mig:planner-sandbox, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/germinate/planner_sandbox/README.md#what-this-ref-impl-must-prove", presence: AVAILABLE, claim: c1-through-c5-reference-mechanisms-exist, evidence_class: landed-mechanism, authority_lifecycle: reference-implementation-only, implementation: partial-sandbox, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3274, disposition: DEFERRED, limitation: not-shippable-germinate-apex, falsifier: claimed-as-full-engine}
  - {row_id: mig:manifest, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:instance.yaml#persona_installer.codex_telegram_bridge", presence: AVAILABLE, claim: desired-bridge-pin-exists, evidence_class: desired, authority_lifecycle: desired-manifest, implementation: code-validated, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision-pin-older-than-current-codex-source, dependency: T-2953, disposition: UNVERIFIED, limitation: desired-pin-is-not-deployed-bytes, falsifier: pin-treated-as-current-runtime}
  - {row_id: mig:manifest-validator, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/persona_installer.py#Manifest.from_bytes", presence: AVAILABLE, claim: programmatic-manifest-validation-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3277, disposition: UNVERIFIED, limitation: standalone-instance-schema-absent, falsifier: claim-of-standalone-schema}
  - {row_id: mig:installer, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/persona_installer.py#PersonaInstaller", presence: AVAILABLE, claim: typed-plan-apply-receipt-rollback-code-exists, evidence_class: landed_mechanism, authority_lifecycle: partial-implementation, implementation: landed-not-germinate-apex, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3191/T-3277, disposition: DEFERRED, limitation: CLI-is-not-full-engine, falsifier: code-existence-treated-as-adoption}
  - {row_id: mig:staged-rollback, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/t2611_recovery_persona_apply.py#rollback_staged", presence: AVAILABLE, claim: checkpoint-owned-inverse-code-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: bounded-historical-only, runtime_observation: unverified, freshness: frozen-revision, dependency: T-3026, disposition: UNVERIFIED, limitation: recovery-fire-pending, falsifier: claimed-as-current-live-rollback}
  - {row_id: mig:cutover, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/persona_cutover.sh#root", presence: AVAILABLE, claim: receiver-and-controller-cutover-script-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: bounded-historical-only, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: no-live-probe, falsifier: claimed-as-current-complete-cutover}
  - {row_id: mig:persona-rollback, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/persona_rollback.sh#root", presence: AVAILABLE, claim: bespoke-restore-and-poller-stop-script-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: bounded-historical-only, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: no-live-probe, falsifier: claimed-as-current-topology}
  - {row_id: mig:controller, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/runtime_controller.py#ensure_viable_runtime", presence: AVAILABLE, claim: ordered-viability-code-exists, evidence_class: landed_mechanism, authority_lifecycle: partial-implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953/T-3108, disposition: UNVERIFIED, limitation: historical-tests-do-not-prove-current-fleet, falsifier: code-means-deployed}
  - {row_id: mig:harness-state, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/harness_state.py#load_active_runtime", presence: AVAILABLE, claim: non-authoritative-runtime-cache-reader-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2985, disposition: UNVERIFIED, limitation: cache-is-not-authority, falsifier: cache-promoted-to-runtime-truth}
  - {row_id: mig:adapter-types, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/harness_adapter/types.py#RuntimeReadinessProof,ReceiverHookReceipt,DeliveryReceipt", presence: AVAILABLE, claim: typed-readiness-receiver-and-delivery-records-exist, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: types-are-not-effects, falsifier: type-existence-proves-path}
  - {row_id: mig:adapter-integration, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/harness_adapter/integration.py#receiver_handoff_actions", presence: AVAILABLE, claim: handoff-action-and-proof-path-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: no-live-exclusive-owner-probe, falsifier: single-owner-inferred-from-code}
  - {row_id: mig:adapter-delivery, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/harness_adapter/delivery.py#record_submitted,inspect_delivery", presence: AVAILABLE, claim: submitted-and-consumed-ack-records-exist, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: ack-is-bounded-before-user-effect, falsifier: ack-equated-to-response-or-effect}
  - {row_id: mig:bridge, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/codex_telegram_bridge.py#ReceiverStateReceipt,mailbox_roundtrip_verify", presence: AVAILABLE, claim: bridge-handoff-and-roundtrip-receipts-exist, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: bounded-historical-only, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2950/T-2953, disposition: UNVERIFIED, limitation: manual-sheet-is-non-authoritative, falsifier: generated-command-treated-as-authority}
  - {row_id: mig:handoff-actions, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/codex_telegram_actions.py#receiver_handoff_actions", presence: AVAILABLE, claim: old-absence-new-owner-order-code-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: exclusive-owner-not-live-probed, falsifier: two-pollers-or-sustained-409}
  - {row_id: mig:poller, source_ref: "codex-config@79f6b4ed8cc7328f38c3fefb1dea44ca3c501a61:codex_telegram/poller.py#run", presence: AVAILABLE, claim: durable-spool-before-offset-and-409-handling-exist, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed-current-source, installed_adopted: unverified, runtime_observation: unverified, freshness: current-source-differs-from-manifest-pin, dependency: T-2953, disposition: UNVERIFIED, limitation: deployed-bytes-unverified, falsifier: cursor-advances-before-spool-or-multiple-owner}
  - {row_id: mig:mailbox, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/mailbox.py#consume", presence: AVAILABLE, claim: at-least-once-handler-ack-code-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: handler-ack-is-not-external-effect, falsifier: cur-entry-treated-as-response}
  - {row_id: mig:mailbox-mcp, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/mailbox_mcp.py#_handle_consume", presence: AVAILABLE, claim: caller-view-consume-surface-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: response-can-be-lost-after-consume, falsifier: exactly-once-claim}
  - {row_id: mig:wake, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/mailbox_wake.py#inject", presence: AVAILABLE, claim: best-effort-wake-injection-code-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: inject-success-is-not-consume-or-ack, falsifier: wake-treated-as-turn-effect}
  - {row_id: mig:mailbox-onboard, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/mailbox_onboard.py#verify", presence: AVAILABLE, claim: mailbox-topology-provision-verifier-exists, evidence_class: landed-mechanism, authority_lifecycle: implementation, implementation: landed, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2953, disposition: UNVERIFIED, limitation: no-live-fleet-probe, falsifier: claimed-as-fleet-provisioned}
  - {row_id: mig:health-core, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/host_control/health.py#HealthSnapshot", presence: AVAILABLE, claim: bounded-health-and-not-configured-model-exists, evidence_class: landed-mechanism, authority_lifecycle: partial-implementation, implementation: landed-scaffold, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652/T-2959, disposition: DEFERRED, limitation: not-whole-fleet-verdict, falsifier: projected-as-global-health}
  - {row_id: mig:health-dbus, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/host_control/health_dbus.py#Health1Object", presence: AVAILABLE, claim: read-only-health-properties-code-exists, evidence_class: landed_mechanism, authority_lifecycle: partial-implementation, implementation: landed-scaffold, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652/T-2959, disposition: DEFERRED, limitation: no-live-service-proof, falsifier: scaffold-treated-as-fleet-health}
  - {row_id: mig:broker-dbus, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tools/host_control/dbus_service.py#HostBroker1Object", presence: AVAILABLE, claim: broker-transport-scaffold-exists, evidence_class: landed_mechanism, authority_lifecycle: partial-implementation, implementation: landed-non-actuating, installed_adopted: unverified, runtime_observation: unverified, freshness: frozen-revision, dependency: T-2652, disposition: DEFERRED, limitation: no-production-actuator-claim, falsifier: scaffold-treated-as-live-broker}
  - {row_id: mig:telegram-server, source_ref: "codex-config@79f6b4ed8cc7328f38c3fefb1dea44ca3c501a61:codex_telegram/server.py#serve", presence: AVAILABLE, claim: send-only-outbound-mcp-server-exists, evidence_class: landed_mechanism, authority_lifecycle: implementation, implementation: landed-current-source, installed_adopted: unverified, runtime_observation: unverified, freshness: current-source-differs-from-manifest-pin, dependency: T-2953, disposition: UNVERIFIED, limitation: no-inbound-authority, falsifier: server-polls-updates}
  - {row_id: mig:t2953, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2953.md#Transition-log", presence: AVAILABLE, claim: component-green-full-path-red-history-exists, evidence_class: open-work-and-historical-observation, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: timestamp-bounded-only, freshness: stale-unless-reprobed, dependency: T-2953, disposition: UNVERIFIED, limitation: ticket-is-not-current-runtime, falsifier: open-ticket-promoted-to-deployment-truth}
  - {row_id: mig:t2970, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/done/T-2970.md#Transition-log", presence: AVAILABLE, claim: production-invocation-and-cross-layer-failure-history, evidence_class: retained-evidence, authority_lifecycle: non-authoritative, implementation: bounded-correction, installed_adopted: bounded-historical-only, runtime_observation: timestamp-bounded-only, freshness: historical, dependency: T-2970, disposition: AVAILABLE, limitation: baseline-failures-retained, falsifier: cited-as-current-all-suite-green}
  - {row_id: mig:t2984, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/done/T-2984.md#Transition-log", presence: AVAILABLE, claim: maintenance-mode-containment-history, evidence_class: retained-evidence, authority_lifecycle: non-authoritative, implementation: bounded-correction, installed_adopted: bounded-historical-only, runtime_observation: timestamp-bounded-only, freshness: historical, dependency: T-2984, disposition: AVAILABLE, limitation: not-fleet-adoption, falsifier: cited-as-global-deployment}
  - {row_id: mig:t2652, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2652.md#Ask", presence: AVAILABLE, claim: host-broker-build-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-2652, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t2667, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2667.md#Ask", presence: AVAILABLE, claim: germination-registry-build-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: absent, runtime_observation: none, freshness: ticket-revision, dependency: T-2667, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t2950, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2950.md#Ask", presence: AVAILABLE, claim: bridge-runtime-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-2950, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t2955, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2955.md#Ask", presence: AVAILABLE, claim: architecture-visualization-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: not_applicable, runtime_observation: none, freshness: ticket-revision, dependency: T-2955, disposition: DEFERRED, limitation: visualization-is-not-authority, falsifier: promoted-to-runtime-truth}
  - {row_id: mig:t2959, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2959.md#Ask", presence: AVAILABLE, claim: health-monitor-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-2959, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t2985, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-2985.md#Ask", presence: AVAILABLE, claim: harness-state-directory-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-2985, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3026, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3026.md#Ask", presence: AVAILABLE, claim: recovery-rollback-fire-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3026, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3108, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3108.md#Ask", presence: AVAILABLE, claim: runtime-controller-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3108, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3175, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3175.md#Ask", presence: AVAILABLE, claim: continuity-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3175, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3177, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3177.md#Ask", presence: AVAILABLE, claim: harness-swap-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3177, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3191, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3191.md#Ask", presence: AVAILABLE, claim: installer-integration-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3191, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3277, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3277.md#Ask", presence: AVAILABLE, claim: installer-contract-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: incomplete, installed_adopted: unverified, runtime_observation: none, freshness: ticket-revision, dependency: T-3277, disposition: DEFERRED, limitation: ticket-is-not-runtime-state, falsifier: promoted-to-landed-or-deployed}
  - {row_id: mig:t3379, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3379.md#Ask", presence: AVAILABLE, claim: org083-ratification-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: absent, installed_adopted: absent, runtime_observation: none, freshness: ticket-revision, dependency: T-3379, disposition: DEFERRED, limitation: proposed-policy-is-not-ratified, falsifier: promoted-to-canonical-authority}
  - {row_id: mig:t3380, source_ref: "pantheon-config@165a6a6b47dcf47485013a72f73d7a311fed304c:tickets/inbox/T-3380.md#Ask", presence: AVAILABLE, claim: org083-build-gap, evidence_class: open-work, authority_lifecycle: non-authoritative, implementation: absent, installed_adopted: absent, runtime_observation: none, freshness: ticket-revision, dependency: T-3380, disposition: DEFERRED, limitation: evidence-machinery-is-not-built, falsifier: promoted-to-landed-or-deployed}
```

The deferred digest dispositions and unresolved ratification envelope are valid inventory findings, not accepted evidence. They deterministically produce `inventory_blocked`. The downstream build must materialize them before projection emission or executable-mode acceptance; it must not silently replace or ignore them.

## Trace and operator-sheet validation

A trace is complete only when:

1. every referenced object and catalog record resolves at the frozen revisions;
2. every applicable ORG-068 leg and identity, security, credential, receiver, delivery, health, continuity, and rollback obligation occurs or has an authority-backed disposition;
3. every edge's preconditions are present in the trace's monotonic established-condition set before the edge runs, and its postconditions are added afterward;
4. every mutating failure exit reaches owned inverse or authorized compensation;
5. receiver transfer proves the prior owner absent before the new owner starts and proves no sustained 409 afterward;
6. delivery evidence preserves each milestone and ends in an asserted effect when the trace claims end-to-end success;
7. source, schema, graph, request, evidence, and generated-artifact digests are current.

An operator sheet is generated only from an accepted trace. It carries all provenance fields shown above, is `commands_authoritative: false`, may not add, reorder, or omit steps, and refuses generation or execution on drift. It has no write-back surface. A sheet generated from this inventory example remains `not-generated` until the downstream validator and generator exist.

## Architectural exercise evidence

Architectural exercise is not a new coverage score. Once ORG-083 is ratified and built, a node, edge, or trace may reference an ORG-083 `BoundaryEvidenceRecord` or positive `EffectivenessReceipt` that names producer, transport, consumer, reached preconditions, asserted delivered effect, immutable revision, artifact digest, and limitations.

Line, branch, function, or changed-code reachability cannot prove an architectural edge. A mock-only or component-only test cannot prove a production boundary it does not traverse. Until T-3379 and T-3380 land verified machinery, the seam is `DEFERRED` and every repository remains `inventory_only` under ORG-083.

## Migration and drift rules

Each migration row independently records:

- source presence (`AVAILABLE`, `ABSENT`, or `UNAVAILABLE`);
- external disposition (`AVAILABLE`, `UNAVAILABLE`, `DEFERRED`, or `UNVERIFIED`);
- authority lifecycle;
- implementation state;
- installed/adopted state;
- runtime observation state;
- freshness;
- exact dependency, limitation, and falsifier.

`AVAILABLE` means only that the surveyed source existed and was readable. It never means built, installed, running, healthy, or current. `ABSENT` means the source was checked at the frozen revision and did not exist. `UNAVAILABLE` means it could not be inspected. Current live unit, receiver, poller, D-Bus, mailbox, harness, and deployed-byte claims remain `UNVERIFIED` because this policy arc performed no production probes.

Before executable adoption, a downstream build must expand the initial ledger to one conforming row for every manifest/controller/installer/receiver/delivery/health/continuity/rollback surface and every open dependency named by T-2956. Drift refusal includes changed source pins, schema digests, authority digests, desired manifest bytes, installed artifact digests, receiver ownership, evidence expiry, or a ticket used as if it were runtime truth.

## Security boundaries

- Identity comes from the manifest, OS/bus-attested caller, and the applicable identity contract; never caller-supplied prose.
- Credentials remain references, not copied values.
- Planners return data; typed executors alone actuate operations.
- Cross-UID, host-state, D-Bus, receiver-ownership, Telegram, mailbox, health, and continuity crossings are explicit objects in the graph.
- Single-poller ownership is structural: outbound send surfaces never call `getUpdates`; receiver handoff proves old absence before new start; sustained HTTP 409 is split-brain evidence.
- Runtime observations and health evidence are read-only inputs to a projection. They cannot grant capabilities or rewrite desired state.
- Generated artifacts carry no authority and expose no mutation surface.

## Alternatives

1. **Parallel authoritative architecture graph — rejected.** It duplicates ORG-080/081 and atlas writers and makes conflicts irresolvable.
2. **Prose or diagrams only — rejected.** They cannot be traversed or falsified mechanically.
3. **Infer architecture from tests or coverage — rejected.** Observation cannot define intent, ownership, or authority; component reachability does not prove a boundary effect.
4. **Overload the existing atlas — rejected.** Its one-question navigation views do not own runtime preconditions, acknowledgements, failure exits, receipts, or rollback traces.
5. **Atlas-composed orthogonal runtime profile — selected.** It reuses atlas IDs and source pins when they exist, owns only trace semantics, and emits disposable views.

## Build and rollout boundary

Adoption establishes only this contract. It does not:

- ratify ORG-076 or ORG-083;
- claim ORG-080/081 implementation or adoption;
- create the ORG-081 registry or germinate apex;
- ship a schema file, validator, generator, collector, CI check, operator sheet, evidence adapter, or retention store;
- mutate a manifest, service, receiver, mailbox, health surface, or production runtime;
- promote an open ticket to landed or observed state.

After adoption, one implementation child must build the schema, canonicalizer, validator, exact migration inventory, read-only generator, and a bounded non-authoritative pilot. The child depends on T-2667 and carries `ratify-impl`, `build`, and `org-084`. ORG-083 evidence-adapter scope additionally depends on T-3379 and T-3380 or remains deferred. No production mutation belongs in that child without a separately reviewed and supervised production ticket.

## Acceptance and falsifiers

The implementation is not conforming if any of these survives validation:

- duplicate authority, copied authoritative value, or generated write-back;
- unresolved, duplicate, or wrong-type identity or reference;
- a prohibited dependency/order/requires cycle or an unbounded recovery loop;
- desired, declared, landed, installed, observed, evidence, generated, or open-work plane conflation;
- missing or expired applicability disposition;
- missing explicit identity/security/capability, wake-to-turn, receiver, delivery, health, continuity, or rollback edge;
- split receiver ownership or sustained Telegram 409;
- wake, maildir acknowledgment, or turn start treated as response or effect;
- mutating failure without reachable owned inverse or authorized compensation;
- a complete trace that omits an applicable lifecycle or boundary obligation;
- line/branch/function/changed-code coverage treated as edge-effect proof;
- a pin, generated artifact, test, ticket, or code file treated as current runtime truth;
- an operator sheet whose source/request/schema/graph/evidence digest is stale.

## Re-evaluate when

- the atlas gains a machine-readable portfolio index or changes its identity/SourcePin contract;
- ORG-081 or the germinate apex lands and provides an authoritative identity set this inventory currently defers;
- ORG-083 is ratified and built, enabling real exercise references;
- a production-shaped pilot finds a required fact plane, delivery milestone, boundary, or rollback disposition the v1 schema cannot represent;
- a source requires overlapping authority that cannot be resolved by field ownership;
- runtime observations need a retention or freshness contract that belongs in a different authority.

## DECISIONS

- **Intent:** obtain an executable architecture without adding a second authority. **Call:** adopt an atlas-composed, digest-bound runtime-trace profile with exact field ownership and no write-back. **justified-via:** P1, P3, P4, D10.
- **Intent:** prevent component-green evidence from masquerading as full-path success. **Call:** separate eight fact planes and seven delivery milestones, ending effect claims only with positive effect evidence. **justified-via:** P1, P4, D5, D11.
- **Intent:** make lifecycle and recovery mechanically decidable. **Call:** require authority-backed applicability, explicit boundary edges, a concrete trace, and receipt-anchored inverse or compensation for every mutating failure. **justified-via:** P1, P4, D6.
- **Intent:** preserve honest maturity boundaries. **Call:** keep ORG-083 foreign and deferred, distinguish source availability from implementation/adoption/observation/freshness, and leave every live claim unverified without a timestamped probe. **justified-via:** P3, P4, P5, D7, D11.
- **Intent:** preserve the adoption and build chain. **Call:** adopt the reviewed contract under the temporary D3/T-3388 autonomy window, while routing all machinery through a dependency-gated implementation child and independent review. **justified-via:** P5, D2, D3, D7, D9.
