# Repository extraction — carving a formed project into its own repo

**Status:** Drafted 2026-08-26 by Orchestrator (HO) per T-3180 D2-writer contract; implements ORG-055 (`architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md`). Rev-2 addresses T-3180 REBOUND (codex 2026-08-26). Awaiting the operator ratification.

## When to use this

When existing code that already lives inside another repo needs to be **moved into its own repository**, because a component has crossed both of ORG-055's boundary tests:

1. **`contract_ready`** — a named, versioned, testable contract exists (or is being landed together) plus contract evidence.
2. **At least one strong `independence_signal`** — independent build/release/rollback/support cadence; distinct authority, privacy, secret, or supply-chain boundary; distinct stewardship or contribution surface; reuse/substitution across assemblies; different host/device/trust zone/runtime; or materially independent history/dependency/artifact/CI weight.

Rapid interface churn, "normally atomic" changes, shared release identity, and aesthetics are typed `keep_together_reasons` — they do NOT satisfy the second test on their own.

**Do NOT use this SOP for a brand-new project** — that is `new-project-bootstrap.md`. Extraction assumes a canonical source already lives inside another repo and consumers already exist.

**Do NOT use this SOP to change repo hosting or move between remotes only** — that is a mirror/rehost operation, out of scope here.

## Procedure

1. **Confirm the project is formed** — ORG-054 project identity must exist (`project_id`, telos, parent, lifecycle). Extraction cannot create, destroy, or rename a project; it only moves the code that realizes one.

2. **Inventory the canonical source**:
   - **Source paths** — every directory/file that belongs in the extracted repo. The record's `current_source` names ONE containing logical repo + path.
   - **Consumers** — every repo, script, deploy path, CI job, or persona that imports/executes/copies the code today. Grep `repos.yaml`, `repo_registry.py`, install scripts, systemd units, and CI configs. These become entries in `compatibility_impact.affected_assemblies` and `catalogue_changes`.
   - **Data + secrets** — persistent state, credentials, or config the code owns or references. Enter as `migration.data` refs; note anything that must stay behind or move alongside.
   - **History requirement** — decide whether to preserve git history (subtree/filter-import) or start from an initial commit. Enter as `migration.history` refs. Preserving history is the default when the code has non-trivial provenance value.
   - **Ownership + steward** — who owns the code today; who will own it after extraction. The named steward for the extracted component becomes the `upkeep.steward` on the new component record.

3. **Name and test the contract** — the extraction is only justified once the contract is named, versioned, and covered by contract evidence. If a contract does not yet exist, land it in the canonical source repo BEFORE cutting; extraction with an unnamed contract fails ORG-055's first test.

4. **Evaluate split/cohesion evidence** — record which strong `independence_signals` justify the split (per the enumeration in "When to use this") and which typed `keep_together_reasons` (rapid interface churn / normally atomic changes / shared release identity / coordination-only extraction) argue against. If keep-together wins on balance, the decision below records `keep-together` — a valid reviewed outcome, not a failure.

5. **Write the boundary-decision record** — construct a `RepositoryBoundaryDecision` conforming to `schemas/repository-boundary-decision.schema.json`. The required top-level fields are:
   - `schema_version`
   - `decision_id` (stable ref)
   - `project_id` (upstream ORG-054 identity)
   - `component_id` (stable ref to the component this record decides on)
   - `current_source` — one object with `logical_repo` + `path`
   - `contract` — the named/versioned/tested contract description
   - `independence_signals` — enumerated evidenced signals
   - `keep_together_reasons` — enumerated typed reasons (empty for a valid `extract`)
   - `decision` — enum: `extract` | `keep-together` (not "extract"/"keep")
   - `migration` — sub-object with `data` / `history` / `old_path_disposition` / `rollback` string-ref arrays
   - `operational_contract` — sub-object with `install` / `update` / `configure` / `monitor` / `remove` string-ref arrays (per ORG-053 lifecycle)
   - `compatibility_impact` — `affected_assemblies` + `catalogue_changes`
   - `implication_acknowledgements` — refs to acknowledged implications (security/privacy/cost)
   - `upkeep_estimate_refs` — refs to upkeep-estimate obligation entries (NOT a local cadence value; the ORG-061 obligation index owns cadence)
   - `authority` — `decision_ref` + `reviewer_evidence_refs`
   - `re_evaluate_when` — string-ref array of ORG-061 obligation-index entries that trigger re-evaluation (not "annual" or any locally-invented default; the obligation index owns trigger/steward/cadence/state/evidence)

   Storage: the record is **an individually validated artifact per T-3181 approval** — there is NO canonical `decisions/` directory or catalogue-boundary-decisions index. Each record is validated on its own against the schema. Land it wherever your project's convention dictates records live (typically alongside the component record it decides on) and reference it from `authority.decision_ref` and the component record's `provides`/`requires` where relevant.

6. **Prepare the noncanonical repo** — new repo per platform convention (deploy key or PAT per `feedback_github_per_repo_deploy_keys`), `.gitignore` first, Tier-1 test harness per `new-project-bootstrap.md` §Procedure step 3. Do NOT push publicly until step 7 audit clears.

7. **Migrate the code — audit-before-push order**:
   - **First**, materialize the extracted tree locally (subtree/filter-repo or fresh copy per the `migration.history` choice).
   - **Second, audit the local tree BEFORE any remote publication** — verify no secrets, no unrelated paths, no bloated binaries traveled across. If anything sensitive is found, fix locally; do NOT push a tainted repo and then rewrite history to remove secrets (rewrite is unreliable against caches, forks, and remote scanning).
   - **Third**, push to the destination remote only after the local audit clears.

8. **Update catalogues + manifests**:
   - Write the new component record (`ComponentRecord` per `schemas/component-record.schema.json`) with `project_id`, the required top-level fields (`schema_version`, `component_id`, `kind`, `source`, `provides`, `requires`, `compatibility`, `deployment`, `implications`, `upkeep`, `maturity`, `tensions`), and `upkeep.review_obligations` referencing the ORG-061 obligation-index entries that own the record's cadence/state/evidence.
   - Assembly manifests that assemble this component update to reference the new component's identity.
   - Any `repos.yaml` / `repo_registry.py` entries update to include the new repo's access grant + clone-on-demand shape.

9. **Prove lifecycle, smoke, health, provenance**:
   - **Lifecycle** — T-2951 plan/apply/verify/rollback succeeds on the new repo end-to-end.
   - **Smoke** — the extracted code passes its own smoke suite in the new repo (green before consumer switchover).
   - **Health** — the referenced health-evidence obligations fire clean.
   - **Provenance** — the boundary-decision record's `authority.decision_ref` and migration commits are discoverable from both the new repo and the record itself.

10. **Cut the canonical source once + update consumers atomically** — remove the extracted paths from the source repo in a single commit that names the extraction (ticket id + destination repo + boundary-decision `decision_id`). Consumers that imported from those paths must be updated in the SAME transaction across all affected repos — otherwise the source repo is broken between cut and consumer-update. Coordinate with `assembly-update.md` for any assembly whose lock references the affected component (provider-compatible-first, consumer-second, then narrow — per that SOP).

11. **Disposition the old path** — enumerated in the record's `migration.old_path_disposition`. Typical shapes: clean removal (no stub; migration commit + boundary-decision record are the audit trail) OR a preserved stub (README.md naming the new home + the ticket, no code) when consumers cannot be updated atomically. Delete the stub once all consumers migrate.

12. **Bind rollback to real affected refs — restore-source-first, then consumers, then destination-decommission**:
   - `migration.rollback` enumerates the exact refs and receipts to reverse: source-repo cut commit(s), destination-repo initial commit + all published tags/branches, `repos.yaml`/`repo_registry.py` entries, component record write, and every consumer/assembly-lock update that touched the extracted component's identity.
   - **The rollback ORDER is NOT the naive reverse-of-forward.** A naive reverse-of-forward would restore consumers first while the old source path is still absent — the restored consumers would point at nothing, and after destination deletion neither realization would be usable. The correct order:
     1. **Restore the old-source realization FIRST.** Revert the source-cut commit so the code exists at the old path again. Verify smoke green at the old path before touching consumers.
     2. **Then restore consumer imports** (or roll back their assembly-lock updates) so they point at the restored old-source path.
     3. **Keep the destination repository available and consumer-usable throughout** — every consumer must be proven off the destination BEFORE the destination is decommissioned. A consumer still resolving against the destination at decommission-time breaks.
     4. **Then roll back registry/access-grant entries** for the destination.
     5. **Then decommission destination publication** (delete pushed refs / archive destination repo).
   - Alternate coherent order: **atomically restore old-source + consumers together** in a single coordinated transaction, then decommission destination after proving no consumers reach it. Either shape is acceptable; the naive reverse-of-forward is NOT.
   - **Preserve the boundary-decision record + component record — do NOT delete them.** Rollback reverses the operational split; it does not erase the historical authorization or evidence. Retain the original record unchanged and add a supersedure by writing a NEW schema-valid `RepositoryBoundaryDecision` with its own `decision_id`, `decision: keep-together`, and `authority.decision_ref` pointing back to the original extraction record — the pair (original extraction + superseding keep-together) remains individually schema-valid and jointly tells the audit story. Do NOT attempt to modify the original record with an invented `status`/`superseded`/annotation field; `schemas/repository-boundary-decision.schema.json` has `additionalProperties: false` and does not carry such a field — an in-place status update would produce an invalid record. P4 requires the durable audit trail; ORG-055's contract-migration-compatibility-rollback-ownership-old-path-disposition record set must remain discoverable.
   - Prove the rollback path pre-commit: dry-run the correct sequence in a scratch environment before landing step 10. A rollback plan you have not exercised is not evidence.

13. **Re-evaluation triggers on the boundary record** — `re_evaluate_when` is populated with stable refs to ORG-061 obligation-index entries. These entries own the actual trigger conditions (event kinds, thresholds, review cadences). The SOP does NOT invent local defaults; every re-evaluation trigger enters through the obligation index, which is where cadence/state/evidence live.

## Common failure modes

- **Extracting without a named contract** — the split fails ORG-055's first test. Fix: land the contract in the canonical source first, then extract.

- **Extracting without a strong independence signal** — the split is aesthetic (repo feels "too big") but the code moves atomically with its neighbors. Fix: STOP; the decision is `keep-together` with the cohesion reasons enumerated in `keep_together_reasons`.

- **Boundary record with invented fields (destination, cadence, etc.)** — writing fields the schema does not have. `schemas/repository-boundary-decision.schema.json` has `additionalProperties: false` at every level; the record refuses validation. Fix: use ONLY the enumerated fields in step 5. Cadence goes into the ORG-061 obligation index that `re_evaluate_when` refs; a "destination" ref belongs in `migration` (the new location's identity) not as a distinct field.

- **Consumers left broken between cut and update** — the source repo compiles/runs fine before the migration commit but breaks immediately after because a downstream consumer still imports the old path. Fix: update every consumer in the SAME transaction; if that's not possible, use the stub disposition from step 11 as a bridge.

- **Secrets or credentials travel across the FIRST push** — an operator pushed the destination repo before running the audit at step 7. Rewriting history to remove secrets after they've reached a remote is unreliable (caches, forks, scanners have already read them). Fix: audit-before-push is non-negotiable per step 7.

- **History carried too broadly** — `git filter-repo` without narrow `--path` filters brings the entire source history into the new repo. Fix: use narrow paths; verify the resulting repo's history mentions only the extracted code.

- **Boundary record written after the migration** — the extraction happens first, the decision record backfilled later. Fix: land the boundary-decision record BEFORE the migration commit — the record is the authorization to migrate.

- **Rollback claimed as "one git revert"** — the operator writes `migration.rollback: [<source-cut-commit>]` and calls it done. That is not sufficient; a completed extraction has touched multiple repos, published refs, registry state, consumer locks, and destination publication. Fix: enumerate every affected ref; sequence the reverse-order rollback; dry-run it before committing.

- **Re-evaluation cadence invented locally** — the operator writes "review annually" into the record or SOP. That duplicates authority ORG-061 owns. Fix: create/reference an ORG-061 obligation-index entry with the trigger; enter its ref into `re_evaluate_when`.

## Boundary

This SOP covers **carving code out of an existing repo into a dedicated repo**. It does NOT cover:

- **Project formation** — that's ORG-054 project identity, upstream of any repo work.
- **Bootstrap of a brand-new project** — `sop/standard/new-project-bootstrap.md`.
- **Assembly composition or update** — `sop/standard/assembly-update.md`.
- **Ongoing catalogue maintenance** — `sop/standard/component-catalogue-upkeep.md`.
- **Feature/capability decommission** — `sop/standard/feature-usage-monitoring-and-pruning.md` (operator-gated).
- **Repository hosting / remote migration** — a different class of operation.

Extraction is a **one-time move**; the resulting repo enters ongoing catalogue upkeep the moment the migration commit lands.

## Cross-references

- `architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md` — the ratified authority for repository boundary decisions.
- `architecture/adr/ORG-054-project-lifecycle-and-purpose-chain.md` — project identity/formation (precedes repo work).
- `architecture/adr/ORG-061-*.md` — owned obligation index (owns cadence/state/evidence, referenced by `re_evaluate_when` and `upkeep.review_obligations`).
- `architecture/adr/ORG-046-*.md` + T-1996 — logical repo resolver contract (adapter seam only in extraction work).
- `sop/standard/new-project-bootstrap.md` — the SOP for a brand-new project's first repo.
- `sop/standard/assembly-update.md` — how assemblies that include the extracted component update.
- `sop/standard/component-catalogue-upkeep.md` — ongoing maintenance of the boundary-decision record + component record.
- `sop/standard/feature-usage-monitoring-and-pruning.md` — where pruning decisions route (do NOT duplicate that authority here).
- `schemas/repository-boundary-decision.schema.json` — the canonical record schema (`additionalProperties: false`).
- `schemas/component-record.schema.json` — the canonical component-record schema.
- `tickets/plans/T-2415-implementation-plan.md` — parent plan authority.
- D5 (smoke verification) — the green-before-cut discipline this SOP extends to migration.
