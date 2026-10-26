# Project lifecycle — formation through retirement

**Status:** Drafted 2026-09-13 by Orchestrator (HO) per T-3166 D2-writer contract (exact amendment specified in `tickets/plans/T-2414-implementation-plan.md` §Exact D2-writer amendment contract); implements ORG-054 (`architecture/adr/ORG-054-project-ontology-and-upward-justification.md`). Awaiting the operator ratification.

## When to use this

Whenever a project — in the ORG-054 sense (a bounded developmental system defined by telos, boundary, interfaces, stewardship, lifecycle, evidence, tensions, and operational obligations; **not** a repository, path, persona, daemon, backlog, or release) — is formed, changes developmental mode, reorients, is replaced, splits, merges, or retires/closes.

This SOP owns **identity and lifecycle state**. It does NOT own repository mechanics:

- **Bootstrapping a formed project's first repository** → `sop/standard/new-project-bootstrap.md`. That SOP's Procedure now opens with a mandatory precondition pointing back here (§Formation below).
- **Carving existing code into its own dedicated repository** → `sop/standard/repository-extraction.md`.
- **Component/assembly compatibility mechanics** (a separate axis per ORG-055) → `sop/standard/assembly-update.md` / `sop/standard/component-catalogue-upkeep.md`.
- **Ticket/brief/reviewer mechanical field enforcement** for `project_id` / `nearest_intent_ref` (parser, CLI, lint) — owned by sibling implementation children (T-3167/T-3168/T-3169), not this SOP.

A project record's required fields (per ORG-054 §1) are `project_id`, `telos`, `parent_goal_ref` (nullable), `boundary`, `interfaces`, `stewardship`, `lifecycle` (current developmental mode + receipt refs), `evidence`, `tensions`, and `operational_contract`. Every section below operates on that record.

## 1. Formation

Creating a project is **goal-level when it introduces a new telos** (ORG-054 §5) — it follows `rulebook/goals.md`'s registered-goal test and the D3 autonomy bar, not below-the-bar autonomous adoption. Before the first implementation arc, a persistent project requires:

- **Formation-vs-probe classification, screened first.** A **bounded exploratory probe** may precede formation when it is labeled as a probe, has no persistent deployment or independent obligations, and names the ticket intent that bounds it. Reuse, handoff, persistence, or a new telos triggers formation before anything else proceeds (this is the same exception `new-project-bootstrap.md` names for repository work — it is a project-formation exception, not a repository one).
- **Canonical project record** — the ORG-054 field set above: stable `project_id`, `telos`, `boundary`, `interfaces`, `stewardship`, an initial developmental mode (below), and `operational_contract` (deployment/data/security/cost/maintenance/recovery obligations).
- **Explicit nullable `parent_goal_ref`.** Joining a composed program requires the parent's decision (the parent project's steward or the operator, per that program's authority). A standalone project records `parent_goal_ref: null`. No tooling or framework default supplies a root — see §8.
- **Formation receipt** (schema below, §9) with `transition: form`, `from_mode: null`, and `to_mode` set to the project's initial developmental mode (typically `shaping`).
- **Hand off to repository bootstrap only if a repository is warranted.** An embedded project (no independent build/release/support cadence, no distinct authority boundary) legitimately never gets its own repo — formation does not imply repository work; see `new-project-bootstrap.md` §When to use this cases 1-2.

## 2. Developmental-mode transition

A project record carries **exactly one current developmental mode** at any time, one of: `shaping`, `infolding`, `infilling`, `operating`, `reorienting`, `retiring`, `splitting`, `merging` (ORG-054 §4's `shaping / infolding / infilling / operating / reorienting / retiring-splitting-merging` enumeration, expanded to the three distinct terminal transition kinds the receipt schema in §9 tracks separately).

A mode change requires:

- **Reason** — the concrete, outcome-language reason the mode no longer fits (not a ticket-count or release-tag observation).
- **Evidence** — health, user-value, safety, or developmental-contribution evidence per the project's `evidence` field, not a bare assertion.
- **Authority** — the project's steward for a below-bar transition; a goal-level ruling (the operator / D3 bar) when the transition itself changes telos or parentage.
- **Tension changes** — if the project's `tensions` field names a pole this transition rebalances, name the trigger that fired (ORG-054 §1 `tensions`; P6 — tensions are preserved, not silently collapsed).
- **Transition receipt** (§9) with `transition: mode-change`, accurate `from_mode`/`to_mode`.

**Developmental mode is never derived from ticket state or release maturity** (ORG-054 §4, explicit). A project with zero open tickets or a tagged v1.0 release can be `operating`, `infilling`, or anything else — inbox/active/done and release tags are ticket-state and packaging facts, not project developmental facts. A reviewer or steward who infers mode from ticket counts has made an unfounded inference, not read the project record.

## 3. Reorientation

Reorientation is a deliberate change to a project's direction while its identity persists (not a replacement, split, merge, or retirement — see §§4-7 for those). It requires:

- **Trigger and evidence** — the health/value/safety signal or the specific tension-rebalance trigger (per the project's `tensions` field) that makes the current direction insufficient.
- **Affected boundaries and modes named explicitly** — a reorientation does not silently widen scope beyond what it names; if boundary or mode changes, both are stated, not left to be inferred from the resulting diff.
- **Preserved tensions** — reorientation rebalances a named pole; it does not collapse a tension that remains genuinely live on other axes (P6).
- **Upward-fit review** — does the reoriented project still serve its `parent_goal_ref` (if any)? A reorientation that no longer fits its parent's telos is a signal for the parent's own steward, not a unilateral local call.
- **Goal-level ruling when applicable** — if the reorientation would itself introduce a new telos or sever/change a `parent_goal_ref`, that crosses into Formation (§1) or Replacement (§4) territory and needs the same goal-level authority those sections require. Reorientation is not a side door around the goal-level bar.
- **Transition receipt** (§9) with `transition: reorient`.

## 4. Replacement

Replacement preserves project identity (`project_id` unchanged) while implementation changes underneath it — a rewrite, a harness swap, a replatform. It requires:

- **Identity preserved** — the same `project_id`, `telos`, and `parent_goal_ref` carry forward unless the replacement is itself also a reorientation (§3) or split/merge (§§5-6), in which case that section's requirements apply in addition.
- **Transfer of runtime, data, and operations with rollback and evidence** — every obligation the old implementation carried (per `operational_contract`) is explicitly transferred, retired-with-justification, or proven unnecessary; the transfer names its rollback path and the evidence that the new implementation actually carries the transferred obligation.
- **Obligation dispositions are not optional for replacement.** A transition receipt for `transition: replace` **cannot complete with an empty `obligation_dispositions` list** unless a reviewer verifies and records that the project carried no obligations to transfer — the same non-empty gate ORG-054's split/merge/retire transitions carry (§9). Replacement is not exempt from the continuity rule: a rewrite that silently drops an operational, data, authority, privacy, cost, user, or recovery obligation is a closure-shaped failure wearing a replacement label.
- **Transition receipt** (§9) with `transition: replace`.

## 5. Split

A split divides one project's identity, boundary, and obligations across two or more successor projects. It requires:

- **Successor `project_id`s and teloi named explicitly** — a split that leaves a successor's identity implicit is not complete.
- **Every obligation allocated** — interface, data, authority, cost, maintenance, and recovery obligations from the original `operational_contract` are each assigned to a named successor; none are left unowned.
- **Supersession map** — a durable record of which original responsibilities map to which successor, so a later reader can trace lineage without reconstructing it from commit history.
- **Rollback boundary** — the split names what can and cannot be reversed (a split that has already diverged successor codebases independently may have a narrower rollback boundary than one caught early; name it rather than assuming full reversibility).
- **Transition receipt** (§9) with `transition: split`, `successor_project_refs` populated, and `obligation_dispositions` non-empty unless a reviewer verifies the original project carried no obligations.

## 6. Merge

A merge combines two or more projects into one surviving or newly-formed identity. It requires:

- **Surviving or new identity named explicitly** — one merging project's `project_id` survives, or a genuinely new `project_id` is formed (per §1's formation requirements); either is valid, but the choice is stated, not left implicit in whichever repository happened to receive the code.
- **Reconciliation** of teloi, `parent_goal_ref`s, boundaries, authority, history, and obligations of every merging project into the surviving/new record. A merge that reconciles code but not telos/boundary/authority has not merged the *projects* — it has merged their repositories.
- **Repository merge alone is insufficient** — combining git histories or codebases is evidence for a merge decision, not the decision itself.
- **Transition receipt** (§9) with `transition: merge`, `successor_project_refs` naming the surviving/new identity, and `obligation_dispositions` covering every merged project's obligations.

## 7. Retirement / closure

A project closes only when its obligations are transferred, retired, or proven unnecessary (ORG-054 §5). Retirement requires disposition of **every** operational, data, authority, privacy, cost, user, and recovery obligation the project's `operational_contract` and evidence trail carry.

**An empty backlog, an archived repository, or a stopped daemon/service is evidence input only — none of these alone constitutes closure.** A continuing organ with no active build work remains `operating`; only a recorded, disposed obligation set closes it.

- **Transition receipt** (§9) with `transition: retire` and `obligation_dispositions` non-empty unless a reviewer verifies and records that the project carried no obligations at closure.

## 8. Rootless and multi-root acceptance

These are ordinary, first-class cases — not edge cases requiring special justification:

- **Null `parent_goal_ref` is ordinary.** A standalone top-level project records `parent_goal_ref: null` at formation and never needs to justify the absence of a parent.
- **Multiple independent roots in one instance are valid.** A Pantheon instance may have several unrelated top-level projects with no shared root north star; nothing forces composition into one Grand Project.
- **Zero formed projects is a valid instance state** — a fresh bring-up, or an instance whose work is entirely embedded/unmapped, has not failed to comply with this SOP.
- **No tool, template, or default ever supplies a root.** An unformed or ambiguous project never silently inherits a default telos, parent, or instance root — see the T-3167/T-3169 substrate work, which must prove exactly this as a fixture, not assume it.

## 9. Receipts and review

Every lifecycle transition (form / mode-change / reorient / replace / split / merge / retire) produces an immutable transition receipt:

```yaml
schema_version: 1
project_id: stable-kebab-id
transition: form | mode-change | reorient | replace | split | merge | retire
from_mode: null | shaping | infolding | infilling | operating | reorienting | retiring | splitting | merging
to_mode: shaping | infolding | infilling | operating | reorienting | retiring | splitting | merging
reason: concise outcome-language reason
evidence_refs: []
authority_ref: canonical decision or role authority
obligation_dispositions: []
successor_project_refs: []
created_at: RFC3339 timestamp
```

- **Formation uses `from_mode: null`** (§1).
- **Split/merge/retire — and, per §4, replace — cannot complete with an empty `obligation_dispositions` list** unless a reviewer verifies and records that the project carried no obligations.
- **The project record updates only after the receipt is durably written and validated** — a transition is not "in effect" the moment someone decides it; it is in effect once its receipt exists and the record reflects it.
- **Independent review** is required whenever a transition changes boundaries — reorientation that widens/narrows boundary, any split, any merge, any replacement that moves obligations, any retirement. This is the same posture `sop/standard/pre-implementation-reviewer-discipline.md`'s Project/purpose chain check applies at the ticket level; a lifecycle transition gets the equivalent check at the transition level.
- **the operator gate** — a transition that creates or changes a telos, or creates/changes a `parent_goal_ref` relationship-authority (attaching, detaching, or reparenting), is goal-level per `rulebook/directives.md` D3 and is not self-ratified below that bar.

## Boundary

This SOP covers **project identity and lifecycle state** — formation, developmental-mode transition, reorientation, replacement, split, merge, and retirement/closure, plus the receipt/review discipline that makes each transition an auditable artifact. It does NOT cover:

- **Repository bootstrap mechanics** — `sop/standard/new-project-bootstrap.md`.
- **Repository extraction mechanics** — `sop/standard/repository-extraction.md`.
- **Component/assembly compatibility** (ORG-055's separate axis) — `sop/standard/assembly-update.md` / `sop/standard/component-catalogue-upkeep.md`.
- **Mechanical `project_id`/`nearest_intent_ref` schema, parser, CLI, lint, or catalogue tooling** — owned by T-3167/T-3168/T-3169, not this SOP.
- **Genuinely unmapped/goal-shaped work** that maps to no project or registered goal — that follows the existing `rulebook/policies/goal-mapping.md` unmapped-work rule; this SOP does not force such work into a project record after the fact.

## Cross-references

- `architecture/adr/ORG-054-project-ontology-and-upward-justification.md` — the ratified authority this SOP operationalizes.
- `architecture/adr/ORG-053-grand-project-architecture-program.md` — the program-level root context (rootless/multi-root instances remain valid; §8 above).
- `architecture/adr/ORG-061-developmental-sequence-evidence-and-reorientation.md` — developmental-sequence/evidence authority feeding reorientation (§3) and evidence-obligation cadence.
- `architecture/adr/ORG-034-two-layer-goal-architecture-intent-first-justification.md` — the intent/telos chain this SOP's project teloi nest under.
- `rulebook/goals.md` — north stars; the D3 goal-level test §§1-9 above route through.
- `rulebook/directives.md` D3 (autonomy bar — goal-level ruling triggers), D6 (plan-first + locked-answer-deviation handling).
- `rulebook/policies/goal-mapping.md` — where genuinely unmapped/goal-shaped work routes instead of a forced project record.
- `sop/standard/new-project-bootstrap.md` — repository-bootstrap procedure that this SOP's Formation section (§1) precedes.
- `sop/standard/repository-extraction.md` — repository-carving procedure; confirms project formation before extraction.
- `sop/standard/team-orchestrator-brief-construction.md` — brief-construction contract that names `project_id`/`nearest_intent_ref`/chain terminus/developmental mode per this SOP.
- `sop/standard/pre-implementation-reviewer-discipline.md` — the Project/purpose chain check that mechanically resolves against this SOP's records.
- `tickets/plans/T-2414-implementation-plan.md` — parent plan authority; §Exact D2-writer amendment contract is this file's specification.
