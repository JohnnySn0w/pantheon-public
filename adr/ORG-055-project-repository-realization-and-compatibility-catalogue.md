---
aliases: [ORG-055]
id: ORG-055
title: "Project-to-repository realization, assembly manifests, and compatibility catalogue"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2415
---

# ORG-055 — Project-to-repository realization and compatibility catalogue

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-028/029/039/046/049. ORG-046 remains Proposed; this ADR does not
ratify it. **Scope:** when code/data becomes an independent repository and how a
seed declares a compatible assembly.

## Context

Grand Project organs need independent development and substitution without
turning every change into cross-repo ceremony or letting a seed assemble an
untested set of versions. The operator prefers linked sub-repositories but the system
has no general boundary rule. ORG-028's no-submodule ruling is specific to
per-harness config composed at the home level; it is not a universal ban.

## Decision proposal

### 1. Project and repository are separate objects

Repositories are source-distribution, release, ownership, and security
boundaries. Projects are telos and lifecycle boundaries (ORG-054). Catalogue
records map them many-to-many.

### 2. Extract a repository only at a contract plus an independence signal

A component is a candidate for its own repository when its boundary has a
named, testable contract **and** at least one strong independence signal:

- independent build, release, rollback, or support cadence;
- distinct authority, secrets, privacy, or supply-chain boundary;
- distinct steward/ownership or contribution surface;
- reuse or substitution by more than one assembly;
- deployment on a different host, device, trust zone, or runtime;
- history, dependencies, artifacts, or CI weight that materially burden the
  containing repository.

Keep components together while their interface is still changing rapidly,
their changes must normally land atomically, their release identity is shared,
or extraction would create only coordination with no independent lifecycle.

Security separation and independent deployment are strong split signals.
Convenience, directory size, and aesthetic purity are weak signals alone.

Every extraction records contract, migration, compatibility, rollback,
ownership, and old-path disposition. Splits do not silently duplicate the
canonical source.

### 3. Use manifest plus generated lock as the assembly contract

A thin Grand Project/seed repository carries:

- architecture and project/component catalogue references;
- a **manifest** of logical component identities, accepted contract and version
  ranges, required feature sets, and resolution sources;
- a generated **lock** of exact source revisions, artifact digests, schemas,
  and the test evidence for one known assembly;
- installer entry points that resolve, verify, plan, apply, verify, and roll
  back under ORG-049–052.

The manifest expresses a compatible possibility; the lock expresses a tested
commitment. Editing the generated lock by hand is invalid. A seed may track a
range but production deployment always records an exact resolved set.

Logical repository names must not be raw URLs. If ORG-046 is later ratified,
its resolver becomes the URL/failover implementation; until then, this decision
only fixes the logical separation and lock evidence.

### 4. Make submodules optional views, not dependency truth

Git submodules may be used when developers materially benefit from an exact
nested source checkout and the host tooling supports it. They do not replace
the manifest, lock, compatibility catalogue, installer, or artifact signature.
An assembly must remain intelligible without recursive Git state.

Per-harness config keeps ORG-028's existing home-level composition unless that
ADR is separately amended. Other organ repositories are not constrained by its
local decision.

### 5. Adopt one compatibility/upkeep record shape

Each component catalogue record contains:

```yaml
component_id: stable-logical-id
project_id: parent-project-id
kind: service|library|config|model|device-adapter|data-store|interface
source:
  logical_repo: logical-name
  path: optional/component/path
  artifact: optional-release-identity
provides:
  - capability: stable-capability-id
    contract: schema-or-interface-id
    version: supported-version
requires:
  hard: []
  optional: []
compatibility:
  accepts: []
  conflicts: []
  tested_revision: exact-lock-reference
deployment:
  loci: []
  trust_zone: named-zone
implications:
  authority: []
  privacy_data: []
  hardware_network: []
  operational: []
upkeep:
  steward: role-or-project
  health_evidence: []
  monitoring: []
  upgrade_rollback: []
  credential_rotation: []
  storage_retention: []
  cost: []
  review_obligations: [owned-obligation-id]
maturity:
  developmental_mode: shaping|infolding|infilling|operating|reorienting|retiring
  replaceability: required|preferred|fixed-with-reason
tensions: []
```

`review_obligations` references the owned architecture re-evaluation index
defined by ORG-061. Trigger semantics, steward, observable source, cadence,
state, and evidence stay in that index rather than becoming free-form catalogue
strings or duplicated normative truth.

Catalogue data is descriptive and reviewable. It may point to richer canonical
records but must not copy secrets, full interface schemas, runbooks, or live
health logs. Generated indexes and validation prevent drift.

### 6. Compatibility is feature- and consequence-aware

Version matching alone is insufficient. Resolution must prove:

- required capabilities and interface/schema compatibility;
- authority and secret providers exist at the right scope;
- data retention, privacy, hardware, network, and cost implications are
  acknowledged by the target seed;
- monitoring, recovery, upgrade, and rollback obligations have owners;
- incompatible feature combinations are refused before mutation;
- the exact assembly passed its declared cross-component smoke.

Adding a feature can therefore make a component incompatible with a seed even
when its API version did not change—for example, by adding a camera, cloud
credential, persistent data class, privileged action, or ongoing cost.

## Consequences

- Repository extraction requires a reviewed contract-plus-independent-lifecycle
  record with migration, rollback, ownership, and compatibility evidence.
- Assembly update and catalogue upkeep require explicit procedures for generated
  lock refresh, compatibility resolution, implication acknowledgement,
  cross-component smoke, health evidence, cost, owner, and review obligations.
  Existing feature-usage review remains the canonical pruning/cost procedure.

- Independent organ repos can move at their own cadence while seeds remain
  reproducible and reviewable.
- The catalogue becomes the shared substrate for installers, health surfaces,
  capability discovery, maintenance planning, and future MCP navigation.
- Cross-repo changes explicitly choose compatibility sequencing instead of
  relying on main-branch timing.
- Some catalogue maintenance is unavoidable; generated locks, indexes, schema
  checks, and health evidence keep it structural.

## Re-evaluate when

- **ORG-055:R1** — the first three organ repos are resolved by one seed manifest;
- **ORG-055:R2** — a submodule is proposed as the only source of dependency truth;
- **ORG-055:R3** — catalogue upkeep exceeds the operational value it produces;
- **ORG-055:R4** — ORG-046 is ratified, rejected, or materially redesigned; or
- **ORG-055:R5** — binary/firmware/model artifacts require a registry not expressible through Git references.
## DECISIONS

- **Split at contract plus independent lifecycle — serves replaceable organs
  without coordination theater — justifies via P1 and P6.** Neither monorepo nor
  repo-per-directory is a goal.
- **Manifest expresses ranges; generated lock expresses tested reality — serves
  adaptable and reproducible seeds — justifies via P1 and P4.** The two
  artifacts preserve plasticity/reliability rather than collapsing either.
- **Catalogue implications and upkeep alongside features — serves honest total
  system cost — justifies via P3 and D12.** A capability that omits its security,
  data, or maintenance consequence cannot justify upward.
- **Submodules are optional views — serves the operator's linked-source preference
  without creating hidden dependency truth — justifies via D10 and ORG-028.**
