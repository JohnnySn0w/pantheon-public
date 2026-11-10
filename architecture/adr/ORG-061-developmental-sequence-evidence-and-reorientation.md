---
aliases: [ORG-061]
id: ORG-061
title: "Grand Project developmental sequence, evidence, and reorientation"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2421
# elements: — ORG-065 machine index of "### 6. Operationalize
# re-evaluation as owned obligations" (T-2946 Tranche A, pair 6/8). E6
# mirrors the native section number; text opens with "§6" verbatim.
# Scope: §6 only — other ORG-061 sections are not yet transcribed. Added
# by HO; no obligation content changed.
#
# T-3205 Tranche B addition (2026-08-25): E0 is the reserved
# whole-document element for the T-2421 "elaborate ORG-061 into the
# developmental program" plan ticket (same whole-ADR policy call as the
# other eight T-2413..T-2421 siblings — see T-3205 DECISIONS). Zero
# cannot collide with E6 or any future native mirror of ORG-061's own
# §1-7 / stage-exit-row numbering (natives start at 1). Added by HO; no
# obligation content changed.
elements:
  - {id: E6, text: "§6 Operationalize re-evaluation as owned obligations: every 'Re-evaluate when' bullet gets a stable obligation id; one owned obligation index groups them with operational metadata (trigger_kind, predicate, state, owner) without copying ADR normative prose; existing ticket primitives (not_before/depends_on) are the first execution substrate"}
  - {id: E0, text: "ADR as a whole: Grand Project developmental sequence, evidence, and reorientation — whole-document element for plan/elaboration tickets (ORG-065 pass-2 policy, T-3205)"}
---

# ORG-061 — Developmental sequence, evidence, and reorientation

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Depends conceptually on:** ORG-053–060. **Scope:** order, exit evidence, and
feedback gates. It does not create implementation tickets or enable production.

## Context

The operator supplied a recovery-to-embodiment order. A list alone can become either
rigid theater or an endlessly deferred vision. Each stage therefore needs an
outcome, falsifiable exit evidence, explicit dependencies, and a reorientation
gate where the concrete system may change the next plan.

## Decision proposal

### 1. Adopt the following directional sequence

| Stage | Outcome | Minimum exit evidence |
|---|---|---|
| 1. Harness parity and control recovery | Personas are not trapped in one harness; abstraction restores confident control | Recovery is brought up through the harness-neutral path; communication, role, authority, work continuation, and rollback are proven |
| 2. Installation and seed viability | One coherent installer can birth/repair a seed and persona from declared inputs | Clean-host plan/apply/verify/rollback plus existing-seat convergence; no copied secrets; exact manifest/lock/receipt |
| 3. Persona restoration | The wider organ fleet returns through the same path | Each intended persona hydrates its continuity bundle, passes role/capability/comms smoke, and resumes a bounded commitment |
| 4. Bridge establishment | One coherent face can answer, delegate, and synthesize across hemispheres | Text-first Bridge trial routes both domains, reports degradation honestly, and preserves provenance/authority |
| 5. Echo conversational interface | Voice becomes a dependable first-class path to the Bridge | Static-geometry microphone deployment sustains multi-turn conversation, barge-in, identity/consent boundaries, and spoken results; text fallback remains |
| 6. Exocortex and memory | Interaction can change future context safely | Real conversations exercise retention, consolidation, purpose-bounded retrieval, correction, forgetting, and usefulness evaluation |
| 7. Federation and inter-system communication | Authenticated seeds can converse/work without confusing identity and trust | Ratified passport/identity design, key lifecycle, bounded request/response, revocation, content-trust separation, and supervised exposure |
| 8. Senses and actuation | The whole can observe and affect environments through typed trust boundaries | At least one sensor cluster and one reversible actuator prove provenance, consent, planning, authority, verification, and safe failure through the capability navigator |
| 9. Rich presence | The same identity can inhabit optional visual/physical surfaces | Two replaceable presence surfaces show coherent state, degradation, privacy/action indicators, and no identity fragmentation |
| 10. Adaptive developmental loop | The whole learns and reorients without losing identity or human authority | A traced cycle from observation through memory/evaluation to an approved behavioral/design adaptation demonstrates benefit, rollback, and preserved canon |

The sequence is dependency-oriented, not a prohibition on early probes. A later
stage may run bounded research/prototypes when it reduces uncertainty for an
earlier stage. Production reliance does not advance until its prerequisites'
exit evidence is real.

**Goal-level ordering — Bridge then Echo.** The operator confirmed at Telegram 534
that the older Echo north-star sharpening was Orchestrator's wording which he assented
to at the time, and that the current Grand Project decisions supersede it. The
Stage 4 -> Stage 5 order therefore stands: prove the Bridge's
answer/delegate/synthesize contract text-first, then attach Echo as its
first-class voice substrate. On ratification, the stale Echo sharpening in
`rulebook/goals.md` must be updated without weakening Echo's project north star
as a general voice interface.

### 2. Make every stage exit falsifiable

The outcome table is orientation; the following matrix is the minimum pass
contract. A stage steward proposes sufficiency, an independent reviewer verifies
the evidence, and the operator accepts exits that cross a goal, relationship, human,
public, privileged, federated, sensory, or physical boundary. Any listed
falsifier keeps the stage from `passed` regardless of narrative confidence.

| Stage | Required real-boundary scenario | Required denial/failure/rollback probe | Evidence artifact and sufficiency authority |
|---|---|---|---|
| 1 | Hydrate Recovery through the declared neutral contract and perform one real harness switch with role, active commitment, and two-way comms readback | Remove one required capability and require readiness refusal; inject new-binding failure and prove the prior binding remains authoritative | Signed/hashed plan, handoff, verify, comms, and rollback receipts; harness steward proposes, independent continuity/security reviewer verifies, the operator accepts the live switch |
| 2 | Install one disposable clean host and converge one existing seat from the same manifest/lock | Reject wrong digest/authority before mutation; inject a mid-plan failure and prove owned-path-only rollback plus unchanged foreign state | Manifest, lock, plan, apply/verify/rollback receipts and host inventory diff; installer steward proposes, independent installer/security reviewer verifies, the operator accepts the real-seat first fire |
| 3 | Cold-hydrate every intended persona, read back identity/role/authority/current commitment, complete two-way comms, and resume one bounded task | Corrupt or omit a continuity reference and require `not ready`; prove removal/rebind does not corrupt canonical identity | Per-persona restoration matrix with bundle revisions, smoke receipts, and task result; technical head proposes, independent reviewer verifies, the operator accepts restoration of relationship-facing personas |
| 4 | Exercise direct answer, technical-head delegation, personal-head delegation, ambiguous-authority escalation, and one unavailable-organ response | Inject stale routing/organ outage and require explicit degraded state with no fabricated result or authority bypass | Conversation transcript plus dispatch/result/provenance receipts and scenario verdicts; Bridge steward proposes, independent routing/privacy reviewer verifies, the operator accepts the relationship interface |
| 5 | On static microphone geometry, complete quiet single-turn, multi-turn follow-up, barge-in, speaker/guest boundary, and spoken-result scenarios with text fallback | Disconnect one speech dependency and require bounded degradation/fallback; unauthorized or bystander content must not cross its consent/privilege boundary | Timestamped wet-run audio/event trace, CSM/intent/action receipts, latency/loss measures, privacy verdict; Echo steward proposes, independent Echo/privacy reviewer verifies, the operator accepts voice quality |
| 6 | Perform a remember -> retrieve -> correct -> supersede/forget cycle and a contradictory-evidence consolidation cycle in real Bridge conversation | Attempt unauthorized retrieval and require denial; roll back a harmful derived memory without altering source evidence | Memory envelopes, lineage, retrieval-purpose records, correction/forgetting proof, and before/after usefulness evaluation; Exocortex steward proposes, independent privacy/epistemic reviewer verifies, the operator accepts relational-memory behavior |
| 7 | Two seeds mutually authenticate, exchange one bounded request/result, rotate one key, and preserve identity/content-trust separation | Revoke the presenter and require subsequent handshake refusal; present authenticated but untrusted content and require no implicit authorization | Handshake, key/rotation/revocation, request/result, and denial receipts; federation steward proposes, independent security reviewer verifies, the operator accepts first external exposure |
| 8 | Use one real sensor observation and one reversible actuator through catalogue -> navigator -> native authority -> verified result | Supply stale/low-quality observation, unauthorized effect, and injected actuator failure; require refusal or safe failure plus rollback/recovery | Sensory/actuation envelopes, exact effect plan, authority/approval provenance, device telemetry, and rollback receipt; device steward proposes, independent safety/security reviewer verifies, the operator accepts physical first fire |
| 9 | Render the same identity/conversation state on two replaceable presence surfaces and move interaction between them | Disconnect one surface and require visible degraded state while the other remains authoritative; any conflicting identity/hidden recording state fails | Synchronized state captures, interaction/provenance trace, disconnect/recovery proof, and user observation; Bridge/presence steward proposes, independent continuity/privacy reviewer verifies, the operator accepts presence coherence |
| 10 | Run one pre-registered adaptation from observation through memory/evaluation to a reversible behavior/design change against a baseline | Require no promotion when the benefit criterion is unmet; roll back the change and prove identity canon, authority, and source evidence are unchanged | Experiment registration, baseline/result measures, decision provenance, user/reviewer verdict, and rollback receipt; whole-system steward proposes, independent architecture/safety reviewer verifies, the operator accepts the developmental change |

### 3. Evaluate the whole, not only artifact completion

Each stage reports four evidence classes:

- **functional:** required capabilities work through real boundaries;
- **continuity:** identity, commitments, and relationship context survive the
  relevant restart/swap/migration;
- **trust:** authority, consent, privacy, provenance, denial, rollback, and
  failure behavior are observed;
- **developmental:** user experience and system evidence say whether to infold,
  infill, reorient, defer, split, or retire.

Unit and simulation evidence are necessary but cannot satisfy live-boundary
claims. A supervised first fire is required when a stage reaches a new human,
public, privileged, federated, sensory, or physical boundary.

### 4. Put a reorientation gate after every stage

At each gate, record:

1. what the stage made possible;
2. what the concrete implementation revealed that the plan did not;
3. which tensions moved and which remain;
4. current developmental mode for affected projects;
5. whether the next stage/order/contract still serves the root north star;
6. changes to catalogue implications and upkeep;
7. user ruling required at goal/relationship level.

The gate is a structural dependency, not a ceremonial review. The default
production chain is:

```text
stage evidence/acceptance ticket
  -> reorientation-gate ticket
  -> next-stage production ticket
```

The next-stage production ticket depends on the reorientation-gate ticket.
Gate resolution records all seven items above and cites the evidence index.
Bounded research or prototypes may still run early under §1, but production
reliance cannot bypass the gate.

The default is not automatic roadmap churn. A deviation needs evidence and an
upward-justified ADR/ticket change. Equally, evidence that the current order is
wrong must not be ignored to preserve the document.

### 5. Maintain one stage/evidence index

The Grand Project catalogue points to stage status and exact evidence records;
it does not copy test logs, incidents, or project roadmaps. An index entry names
outcome, owner, state, dependencies, evidence refs, outstanding risks, next
gate, gate ticket ref, reorientation record ref, applicable re-evaluation
obligation refs, and last reorientation. Project repos continue to own their
implementation roadmaps.

### 6. Operationalize re-evaluation as owned obligations

Every `Re-evaluate when` bullet in ORG-053–061 has a stable obligation id. One
owned obligation index groups those ids and records operational metadata without
copying the ADR's normative trigger prose:

```text
ReevaluationObligation {
  id
  source_ref
  owner
  trigger_kind: calendar | milestone | threshold | proposal | incident
  predicate
  observable_source
  state: armed | due | open | satisfied | superseded
  armed_ticket
  last_evaluated
  next_check
  evidence_refs
  outcome_ref
}
```

Existing ticket primitives are the first execution substrate:

- calendar/periodic obligations use a self-addressed `not_before` review ticket
  which must file its successor before resolution;
- ticket-represented milestones use a pre-filed `depends_on` review ticket;
- high-risk proposals and first fires make build/deploy work depend on the
  review ticket; and
- semantic/external triggers name an observable source and steward; a periodic
  sweep is a backstop, not proof that the event was observed.

The evaluator may open or refresh one deduplicated review ticket per due
obligation. It never mutates architecture or accepts a stage. New timers or event
adapters require recorded evidence that `not_before`, `depends_on`, grooming,
and the stage/evidence index cannot cover the need.

A completeness lint verifies both directions between source ids and the owned
index, rejects duplicate ids and stale open obligations, and rejects a `passed`
stage lacking a reorientation receipt. The lint runs unasked in the standing
test suite. Its first implementation includes deliberately stale, missing, and
duplicate known-positive fixtures: each is observed red before the fix and green
after it. A lint that exists but is not automatically run, or has not been shown
to fail, is not architectural insurance.

> Implemented by: T-2426 (2026-08-25, `674bb0caf9`, PR #357): `tools/grand_project_program.py` and `tickets/grand-project-reevaluation-obligations.yaml`, evaluated by the weekly `tools/adr_reeval_walk.py`. The walker files one deduplicated review ticket when an obligation's predicate holds (milestone: `depends_on` the named ticket; calendar: `not_before` = `next_check`; threshold: a metric crossing) rather than pre-filing it, treats proposal and incident obligations as surface-only (all 51 current obligations), and drops index-owned ids from its legacy per-trigger lane.

### 7. Define failure and pause honestly

A stage may be:

- `shaping`, `active`, `evidence-pending`, `passed`, `reorienting`, `paused`, or
  `retired`;
- passed only when all four evidence classes required by its boundary exist;
- paused without implying failure or blocking all independent work;
- reopened when later evidence invalidates its contract.

Token/vendor lockout, unavailable hardware, or absent approval is a dependency
state, not architectural success or failure.

## Consequences

- The immediate recovery work and long-horizon artificial-life goal live in one
  chain without pretending they are the same altitude.
- Progress is visible through capability and relationship evidence rather than
  commit or ticket volume.
- Bounded probes preserve plasticity while stage exits preserve reliability.
- Implementation feedback has a legitimate route to reshape architecture.
- The roadmap can survive changing tools and devices because it names outcomes,
  not vendor milestones.

## Re-evaluate when

- **ORG-061:R1** — any stage passes its first live gate;
- **ORG-061:R2** — a later-stage prerequisite is proven false;
- **ORG-061:R3** — two stages can no longer be sequenced independently;
- **ORG-061:R4** — stage evidence becomes checklist theater;
- **ORG-061:R5** — the user/relationship goal changes; or
- **ORG-061:R6** — stage 10 demonstrates a full adaptive cycle.
## DECISIONS

- **Owned obligations over prose reminders — serves re-evaluation that actually
  fires — justifies via P1, P4, and ORG-012.** Existing time/dependency tickets
  are the first actuation substrate; new scheduling machinery must earn itself.
- **Reorientation receipt gates next-stage production — serves evidence-driven
  development without blocking bounded probes — justifies via P1 and P6.**
- **Standing-suite known-positive lint — serves proof that governance checks can
  fail before their green result is trusted — justifies via D5 and P4.**

- **Outcome/evidence stages over date roadmap — serves structural progress —
  justifies via D12 and P3.** Wall-clock is not the architecture.
- **Four evidence classes — serves a living, trusted whole rather than green
  unit tests — justifies via P4 and ORG-053.**
- **Reorientation after every stage — serves broad/precise mutual correction —
  justifies via P6.**
- **Real-boundary proof for real claims — serves honest readiness — justifies
  via D11 and D5.**
- **One index with references — serves legibility without transclusion —
  justifies via D10.**
