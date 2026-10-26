---
aliases: [ORG-089]
id: ORG-089
title: "Deference heuristic — viability pass on reserved-set operator directives + appetite budget circuit breaker"
status: >-
  Ratified 2026-09-06 by the operator via Telegram 9047 ("looks good tbh"). Rev-3
  landed at commit f9ca0b98b1 after the operator steer TG 9040 to strip the
  impl-tier over-spec that grew across Consultant round-1/2 REBOUND responses;
  keep only design-level claims that invalidate ratifiable-decision altitude.
  Consultant ORG-041 review chain: rev-0 REBOUND round-1, rev-1 REBOUND round-2,
  rev-2 CAP-EXHAUSTED round-3 (superseded by the operator steer at his tier before
  the round-3 verdict reached HO — audit chain preserved in T-3431 transition
  log).
date: 2026-09-05
author: Specialist (proposal + spec) + Orchestrator (ADR authoring + surface diffs)
steward: orchestrator
supersedes: none
relates: ORG-041, ORG-021, ORG-034, D3, D1, P3, P4
build_disposition: needs-build
scoping_ticket: T-3431
registers_components: []
appetite: 7
---

# ORG-089 — Deference heuristic (viability pass on reserved-set operator directives + appetite budget circuit breaker)

**Status:** **RATIFIED 2026-09-06 by the operator via Telegram 9047** ("looks good tbh"). Ratified-in-principle 2026-09-05 (TG 1725), greenlit landing (TG 1742), formal ratify on rev-3 (TG 9047). Authority-calibration change (adjusts how much agents defer to operator wording on the reserved set); goal-level / reserved — the operator's ratify, not autonomous adoption.

**Related:** [[ORG-041]] (planning-phase spec review — surface 2 widens its scope), [[ORG-021]] (ticket-state taxonomy — surface 4 amends it with `DROPPED`), [[ORG-034]] (arc-intent-first — appetite ties to intent shape), D3 (autonomy bar — the trigger surface reuses the reserved-set boundary D3 already defines), D1 (operator-bandwidth preservation — the circuit breaker is what stops bets silently overrunning budget), P3 (decisions justify upward — the fields make justification legible on the artifact), P4 (traceability — the Appetite + Riskiest-assumption fields become audit-chain evidence even when the operator overrides).

## Appetite

**Score: 7/10** (value 8 / strategic 7 / reversibility 6, average 7). One ADR + five surface amendments across two repositories (pantheon-config + echo-prismatic) + calibration warm-up + fleet propagation. Not worth a new standing role or new machinery. If it needs more than that, the design is wrong.

Explicit ceiling: five surface amendments as scoped in §Decision. Any surface adding a new component, a new role, or a new standing process signals appetite overrun — halt + re-scope, don't quietly widen. **Not one atomic commit** (was rev-0's claim, factually impossible across two repos): the landing is a sequenced multi-step transaction, not atomic; each step's rollback boundary is self-contained.

## Riskiest assumption

**That the pass degrades into ritual contrarianism that taxes the operator's bandwidth instead of producing better ideas.** Cheap test / mitigation is baked into the design: layer-1 reserved-set trigger fires the pass only on high-stakes calls, and the "surface only if it would materially change the decision" clause suppresses ritual objection. Re-evaluate at 90 days: if the pass has added digest volume WITHOUT changing outcomes on any of ≥3 material-change flag surfaces, the pass has become ritual and needs either sharper trigger or retirement.

## Context — the gap the pass closes

The one systematic ADR-thinness surfaced by the 2026-09-05 corpus sweep (`6991d9525`, `knowledge/episodic/2026-09-05-adr-content-sweep-thinness/`) is **up-front viability + appetite**, and its strongest predictor is **provenance**: ADRs that transcribe an operator *verbal* directive skip the viability pass. The drafting agent treats the directive as settled and *transcribes* rather than *shapes*. Both of the operator's symptoms are this one missing appetite gate:

- **Broad-strokes ADRs under-invest** — no floor, commit without checking what kills the bet.
- **Over-specified ADRs over-invest** — no ceiling, builder-grade detail ahead of felt pain.

The worst case is the highest-stakes one (ORG-042 named as example), because the biggest structural calls are the ones made verbally. The operator's steer (TG 1712–1717): "a more equitable, not totally equitable" model; "I want pushback and better ideas and considerations"; "how does this get structurally encoded."

## Decision

Separate the **decision** (the *what*, which stays the operator's) from the **shaping-and-viability pass** (the *whether*, *how much*, and *at what altitude*, which always runs). An operator directive above the reserved / goal-level line gets the viability pass run, and any result that would materially change the outcome surfaces as a reasoned lean *before* it is transcribed into an ADR or a committed change. The operator's word still settles the *what*; it no longer auto-settles the *whether-and-how*. He keeps the final call; the pass **informs** it, does not **gate** it. Below the reserved line, agents proceed as they do now (D3 autonomy-bar unchanged).

### 1. Two modes — RECORD (every non-vault ADR) and SURFACE (reserved-set only)

Every non-vault ADR carries populated `appetite:` + §Appetite + §Riskiest assumption sections — the drafting agent runs the three viability questions and records the results on the artifact. **RECORD-mode** is unconditional; it preserves audit-chain evidence and closes the over-investment failure class for below-reserved ADRs (writing the appetite score forces the ceiling question). Does not surface to the operator.

**SURFACE-mode** fires ONLY when (a) the directive touches D3's reserved-set / goal-level boundary (org shape, principles, standing roles, irreversible external / real-world / identity acts, money, access) AND (b) the pass produces a result that would materially change the decision. Then the result surfaces as a reasoned lean before transcribing. Below the reserved line: no surface.

Reusing D3's reserved-set as the SURFACE-mode trigger keeps the boundary in lockstep with the autonomy-bar; no new boundary to maintain.

### 2. The three viability questions (the pass content)

At the trigger, the drafting agent runs three checks before transcribing:

1. **Riskiest assumption + cheap test** — what unknown would kill the bet, and how cheaply can it be probed before commit?
2. **Appetite / is-this-worth-it-now** — what depth and mechanism is warranted right now, measured against outcome value, strategic alignment, and reversibility (per §Appetite scale, canonical anchors at `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`).
3. **Strongest genuine alternative + why-not** — the best other option a serious adversarial reader would raise, and the reason not to take it.

**Material-change filter.** Any result whose surfacing would materially change the outcome gets surfaced as a reasoned lean — *not* as a bare question, and *not* as ritual contrarianism. Results that don't change the outcome stay in the artifact record (Appetite + Riskiest-assumption fields) but don't surface as an operator ping.

### 3. Appetite scale (the metric)

Canonical rubric at `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`. Summary:

- Appetite = average of three 1-10 sub-scores, rounded: **value-if-it-works** + **strategic alignment** + **reversibility**. All three scored positive (higher = more appetite; the average is meaningful).
- **1-3 low:** smallest thing that works, minimal mechanism.
- **4-6 medium:** shaped pitch at the right altitude (problem, rough solution at connection level, named riskiest assumption, no-gos). Right-altitude ADR, not a spec.
- **7-8 high:** valuable, aligned, safe to invest in — go deeper; walking-skeleton if multi-part.
- **9-10 top:** full green light — invest freely, iterate fast.

Reversibility does double duty (sizes investment appetite AND, via the separate reserved-set trigger, fires the viability pass on irreversibility). Keeping those two jobs apart is what lets reversibility sit cleanly in the average.

### 4. Circuit breaker (architecturally enforced, deterministic + periodic)

**Appetite as machine-readable field** on the bet (ticket/ADR frontmatter). Maps to a spend-budget once calibration lands. v1 spend unit = time-in-active (a truer signal — attempts, rebounds, token cost — can replace it later without changing the design).

**Generate-only until T-3430 calibration.** The band→time-budget mapping doesn't exist up front; log appetite/actual/outcome per bet for the warm-up period. T-3430 (`not_before: 2026-12-05`, plus ≥20 bet-outcome preconditions) fits the mapping from real data and flips the switch. Warm-up data lands inside pantheon-config's ledger tree so it fleet-git-syncs by construction — not in per-seat local state.

**Enforcement via ticket substate**, not by digest warning alone. When the breaker fires, the ticket auto-transitions to `substate: appetite-overrun` in-place (per ORG-021's frontmatter-substate convention). What's structurally prevented at that point: new claims (claim refuses), delivery pickup (inbox_consumer skips), resolve (refuses without a re-decision command). What isn't structurally prevented, and this ADR names honestly: in-flight continuation on an already-open claim between the overrun-flag write and the seat's next attend — the harness doesn't expose a mid-turn interrupt to grooming. A best-effort mailbox advisory to the claimant + Orchestrator carries the soft-signal early notification; the digest surfaces the flag for daily lookback.

**Re-decision commands, not resolve overload.** The re-decision surface has three verbs: re-earn (with a new appetite score + rationale, ticket stays active), drop (terminal, moves to `substate: dropped`), adjust-appetite (semantically distinct from re-earn — rationale for why the original score was wrong). Resolve stays terminal-verb; not overloaded.

**Continuation-bias fix (the whole point):** the substate transition + the resolve refusal make the overrun impossible to silently ignore at the resolve-boundary. Continuation-bias fails at the boundary where it matters most; the honest-limitation between-flag-and-next-attend is a smaller residual than rev-0's "digest warning only" would have left.

## Landing bundle (the five surfaces, on the operator's stamp)

Each surface's diff is described here — the actual patches land as a single commit on stamp. All five surfaces bolt onto machinery that already exists; no new standing process or role.

### Surface 1 — RULE: `rulebook/policies/trust-posture.md` §Act-vs-park amendment

**Add clause** to §Act-vs-park calibration:

> **Viability pass on reserved-set operator directives (2026-09-05, ORG-089).** When transcribing an operator directive that touches the reserved / goal-level set (org shape, a principle, a standing role, an irreversible or external act, money, or access) into an ADR or a committed change, the drafter first runs the viability pass (§ORG-089 §Decision.2: riskiest assumption + cheap test; appetite / is-this-worth-it-now; the strongest genuine alternative + why-not) and surfaces any result that would materially change the decision, as a reasoned lean, before transcribing. Below the reserved line, proceed. The operator's decision authority is unchanged; the pass informs it.

### Surface 2 — GATE: [[ORG-041]] planning-phase review widening

**Amend** [[ORG-041]] to add the three viability questions to the adversarial planning-phase review checklist, and make that review **REQUIRED** for reserved / goal-level directives, verbal ones included (they currently skip it). Same ≤3-round loop with surface-on-exhaustion; no new step, widened coverage + sharper checklist. Cross-reference ORG-089 in [[ORG-041]]'s §Related.

### Surface 3 — RECORD: two ADR-template fields

**Amend** `adr/README.md` §Format to require two fields on every non-vault ADR (agent-authored, 200+ in project ADRs; every ORG-NNN in Pantheon):

- **Appetite** — what this is worth NOW, and therefore how much depth and mechanism is warranted (the ceiling against over-specification). Scored per the §Appetite scale rubric (1-10 average across value / strategic alignment / reversibility); frontmatter field name `appetite:` (integer 1-10) with the reasoning-block in the ADR body §Appetite section (see this ADR §Appetite for the shape).
- **Riskiest assumption** — the one unknown that would kill the bet, and the cheap way to test it (the floor against under-viability). Named + cheap-test-shape in a §Riskiest assumption section (see this ADR §Riskiest assumption for the shape).

**Also amend** `echo-prismatic/adr/TEMPLATE.md` (agent-authored template for ADR-200+) to add both sections + frontmatter field. Push from HO (echo-prismatic push authority per T-1673; Specialist deploy key cannot push echo-prismatic).

Both fields become audit-chain evidence even when the operator overrides — the pass ran, the record shows it, alternatives-considered survive.

### Surface 4 — ENFORCEMENT: grooming checks + [[ORG-021]] DROPPED semantics

**4a-completeness.** Grooming (`tools/ticket_grooming.py`, parallel pattern to the existing `build_disposition` zero-ticket backstop) flags any ratified non-vault ADR whose §Appetite or §Riskiest-assumption field is empty or boilerplate. Digest-only, never blocks. Legacy grandfathering by ratify-date: ADRs ratified before ORG-089's landing are exempt.

**4a-overrun.** Grooming also detects appetite-budget overrun (§Decision.4). Fires only in active-mode (gated on calibration file per §Decision.4); silent in generate-only warm-up. On fire, writes `substate: appetite-overrun` in-place + sends mailbox advisory to claimant + Orchestrator (soft-signal per §Decision.4). Fail-safe direction: never block work on data-corruption or missing fields — the breaker's purpose is surfacing overrun for judgment, not blocking legitimate work.

**4b. [[ORG-021]] amendment: `substate: dropped`** as a first-class frontmatter substate on tickets in `tickets/done/`. Per ORG-021's exactly-3-directories + frontmatter-only-future-substates mandate, no new directory. Design claims (build-tier implementer wires these through the code, not this ADR):

- Dropped tickets DO NOT satisfy dependencies (differs from resolved). Downstream `depends_on: [T-<dropped>]` stays gated until either (a) the dropped ticket is reopened + re-earned + shipped, or (b) the dep is manually pruned with rationale.
- Dropped tickets DO NOT satisfy ORG-065 obligations. An obligation whose satisfying ticket was dropped needs re-satisfaction or explicit waiver.
- Reopen path: `ticket.py reopen --re-earn-appetite <N> --rationale "<text>"` clears the substate + restarts the overrun clock with the new budget.
- Distinct list/digest surface: "dropped this week" is a calibration signal, not noise.

The build ticket for the ORG-021 amendment enumerates the consumer-code updates needed to make these claims real (the design claim is that dropped ≠ resolved everywhere; the impl is a completion-predicate refactor across the existing done-as-shipped consumers).

### Surface 5 — ACTOR: SOP + Claude-side priming; Codex reach honestly narrowed

**5a.** New SOP `sop/standard/shaping-and-viability.md`. The how-to for running the pass: three viability questions, material-change filter, surface-shape (reasoned lean, not bare question, not ritual contrarianism), field-shape (§Appetite + §Riskiest-assumption sections + frontmatter integer). The drafter's reference when the trigger fires. Consultant's ORG-041 review checklist points at it.

**5b.** Add `shaping-and-viability` as a policy-trigger cheat-sheet entry in `rulebook/persona-orientation/manifest.yaml` (Claude-adapter path). Re-renders reach every Claude-side drafter + reviewer per T-3339 + T-3432.

**5c. Codex reach.** Codex adapter integration is TBD-phase-2 (per T-3338). Until Phase-2 lands, the manifest re-render doesn't prime Codex/Consultant — the standing ORG-041 reviewer. This ADR does not solve that; the honest disposition is that the Surface 2 amendment to [[ORG-041]] IS Consultant's authoritative surface for this ADR class. Phase-2 follow-up is T-3338 scope, not carried here.

## Consequences

**Positive:**

- Highest-stakes ADRs no longer skip the viability pass. Verbal-directive transcription — the shape that failed hardest per the sweep — gets shaped, not just transcribed.
- Appetite becomes explicit + machine-checkable. Bets that silently overrun stop being invisible to the operator until months later.
- The **DROPPED** state gives continuation-bias a structural counter — a bet that was worth doing at appetite score N and now isn't, gets closed cleanly instead of dragging.
- Alternatives-considered survive on record even when the operator overrides — audit chain gains explicit "the pass ran + here's what it produced + the operator chose X anyway" trail.
- Reuses D3's reserved-set boundary — no new maintenance surface, no new taxonomy for agents to learn.

**Negative:**

- Adds one authoring step to the reserved-set directive-transcription path. If the pass becomes ritual, it's a bandwidth burn on the operator. Mitigation: material-change filter + 90-day re-eval.
- The two new ADR-template fields must actually be populated on every ORG-NNN going forward. If left boilerplate, the field is decorative and the check has no teeth. Mitigation: the grooming lint (surface 4a) is what gives them teeth; it flags boilerplate/empty.
- The DROPPED state adds one more terminal disposition to reason about. Mitigation: it's terminal + digest-visible, so easy to reason about; harder disposition (should we re-earn vs drop) is the point, not accidental complexity.
- Appetite scoring is subjective at v1. Calibration warm-up (T-3430) is deliberate — the scale improves as it earns its calibration data. Meanwhile, the field's *existence* is what matters more than its numeric precision.

**Deferred:**

- Major-component determination / walking-skeleton habit (L2 corpus-sweep's third pillar) — lower priority per the sweep's finding that connection quality is already a fleet strength. Separate arc if it surfaces.
- Focus / prioritization / cadence (L1's biggest-gap flag) — this ADR improves the quality of each decision; it doesn't touch which decisions get made, in what order, or at what rhythm. Separate portfolio-and-cadence fix, flagged for its own pass.
- Truer-than-time spend units (attempts, rebounds, token cost) for the circuit breaker's actual signal. v1 uses ticket-active-age; replacement happens once those signals are available and calibrated.
- Auto-populating `appetite:` on legacy ADRs (grandfathered by the grooming lint; new ADRs only unless a specific ADR gets touched for other reasons).

**Open questions:** none held open here — calibration heuristics arrive via T-3430; DROPPED representation is picked in §Landing bundle §Surface 4b (`substate: dropped` in `tickets/done/` per ORG-021's exactly-3-dirs mandate); trigger/record scope split picked in §Decision.1.

**Relates to tensions:** the "not totally equitable" model the operator flagged — this ADR is the structural encoding of that tension. The pass fires on reserved-set only; below-line agent autonomy stays intact per D3.

## DECISIONS

*Letters (D-A, D-B, …) are this ADR's local decisions; the rulebook's D1, D2, … in `rulebook/directives.md` do not collide.*

- **D-A. Trigger reuses D3's reserved-set / goal-level boundary, not a new taxonomy — D3 (autonomy bar reuse), P1 (structure over policy).** Introducing a new "high-stakes" boundary in parallel to D3 would drift; sharing D3's boundary keeps them in lockstep.
- **D-B. Material-change filter on surfacing — D1 (operator-bandwidth preservation), P6 (tension preservation).** Without the filter, the pass degrades to ritual contrarianism (the riskiest assumption named in §Riskiest assumption). Filter keeps the pass's outputs *informative* rather than *noise-adding*.
- **D-C. Circuit breaker is deterministic + periodic, never in-the-moment LLM — P1 (structural over runtime), the operator TG 1737 (verbatim: "must not depend on the agent noticing mid-work").** Continuation bias makes agent self-assessment on overrun unreliable exactly when it matters most. The grooming pattern is proven; reuse.
- **D-D. Generate-only until T-3430 calibration — the operator TG 1740 (verbatim sequencing).** A budget mapping without calibration data is a made-up number; enforcing on a made-up number produces false-fire noise. Warm-up produces the mapping; assignment turns the check on.
- **D-E. Appetite unit is a 1-10 average of three positive sub-scores — the operator TG 1727/1731/1736 (metrics + revision).** All three scored positive means higher-is-better and average-is-meaningful, unlike a mixed-sign scale.
- **D-F. Reversibility scored as positive (safe-to-undo = more appetite), with the SEPARATE reserved-set trigger firing on irreversibility for the viability pass — appetite-scale.md §Reversibility rationale.** Keeps the two jobs (appetite-sizing vs pass-triggering) architecturally distinct even though both key off reversibility. Attempting to bundle them collapses that distinction and re-introduces the collapse-into-single-number failure mode.
- **D-G. DROPPED is a first-class ticket state, not deprioritize-to-FIFO — ORG-021 amendment; the whole point of the circuit breaker is a re-decision that CAN be "drop."** A drop that hides as "de-prioritized inbox" is exactly the continuation-bias failure this ADR exists to close. Terminal + digest-visible closes it structurally.
- **D-H. Fields are REQUIRED on every non-vault ADR (200+ / ORG-NNN), grandfathered for legacy — P4 (traceability going forward), P7 (cheap protective action: don't mass-edit history for a going-forward norm).** Legacy ADRs stay as-is; the norm attaches to new work.
- **D-I. Skill priming via persona-orientation manifest, not a bespoke skill — D10 (context-frugality), reuse T-3339's rendered pipeline.** Adding shaping-and-viability as a policy trigger cheat-sheet entry means every drafter/reviewer sees it at spawn without a new skill dir.
- **D-J. Formal ratification requires the operator's stamp; adoption in principle (TG 1725) is not the stamp — D3 (authority-calibration change is goal-level / reserved / the operator's).** Even though the operator greenlit landing (TG 1742), the actual ADR text + surface diffs are what he ratifies. Surfacing the bundle for stamp.
- **D-K. Ship as an ordered multi-repo transaction, not one atomic commit.** Pantheon-config surfaces + code land in that repo's own sequenced commits; echo-prismatic TEMPLATE.md lands separately in that repo; fleet re-render is non-transactional per-seat. Each step's rollback boundary is self-contained (Step 1's surfaces work for Pantheon-scope ADRs even before echo-prismatic lands; echo-prismatic is additive for its own scope). Rev-0's "one commit" was factually impossible across two repos.
- **D-L. Two modes — RECORD (every non-vault ADR) + SURFACE (reserved-set only).** Rev-0 conflated them; below-reserved over-investment failure cases would have missed the field. Splitting fixes coverage: every ADR earns the record (audit + calibration data); only reserved-set surfaces to the operator (bandwidth preserved).
- **D-M. Enforcing gate is a ticket substate transition (`appetite-overrun`), and the resolve-boundary limitation is honestly named — P1 (structural over prose promise).** The substate refuses new claim + delivery skip + resolve. Between overrun-flag and next-attend, in-flight continuation on the current open claim is unavoidable without a mid-turn interrupt the harness doesn't expose to grooming. Best-effort mailbox advisory as soft-signal + digest surface. This is the honest ceiling; rev-0 over-claimed mid-work interruption.
- **D-N. `substate: dropped` frontmatter (in `tickets/done/`), not a 4th directory.** ORG-021 mandates exactly 3 directories + frontmatter-only future substates; that's the binding constraint. Dropped ≠ resolved: no dependency satisfaction, no obligation satisfaction, distinct digest surface, reopen-by-re-earn. Implementation of the completion-predicate refactor is build-tier scope, not this ADR.
- **D-O. Completeness check + overrun check are distinct grooming surfaces.** Proposal notes.md:60-64 required both; collapsing them loses the completeness signal. Completeness is always-on (legacy-grandfathered by ratify-date); overrun is calibration-gated.
- **D-P. Re-decision commands are dedicated verbs (re-earn / drop / adjust-appetite), not resolve-overloaded.** Resolve stays terminal. The three re-decision verbs each carry a rationale + a state effect (re-earn/adjust: stays active with new budget; drop: terminal). Preserves semantic hygiene.
- **D-Q. Codex/Consultant reach is via ORG-041 amendment (Surface 2), not persona-orientation manifest re-render.** Codex adapter is TBD-phase-2 (T-3338); the manifest can't prime Consultant today. The Surface 2 amendment to ORG-041 IS Consultant's authoritative surface for this ADR class. Phase-2 follow-up filed under T-3338.

## Build plan

**On the operator's stamp**, land as an ordered multi-step transaction (per D-K). Each surface is a build ticket owned by the tier that implements it; this ADR names the design intent, not the implementation contract.

- **pantheon-config** (surfaces 1, 2, 3-Pantheon, 4a-completeness, 4a-overrun, 4b, 5a, 5b) — sequenced commits with tests for the code surfaces. Warm-up store lands at `ledger/appetite/<year>-W<week>.jsonl` inside the repo (fleet-git-synced by construction). New ticket-tier verbs (re-earn / drop / adjust-appetite / reopen --re-earn-appetite) + `substate: appetite-overrun` + `substate: dropped` frontmatter shipped alongside the grooming checks that use them.
- **echo-prismatic** (surface 3-echo) — TEMPLATE.md gains the same `appetite:` frontmatter + §Appetite / §Riskiest assumption sections. Separate repo commit; sequenced after pantheon-config lands + verifies.
- **pantheon-pwa** (surface 4b-adjacent) — ticket-list view gets a distinct visual state for dropped tickets (differs from resolved).
- **fleet propagation** (surface 5b-adjacent) — per-seat render.py pass so the manifest change reaches Claude-side drafters + reviewers. Non-transactional; per T-3432 durable-mechanism when it lands, HO-manual until then.

**Post-landing:**

- Log appetite + actual + outcome per new bet into `ledger/appetite/` for the calibration warm-up. T-3430 (`not_before: 2026-12-05`, plus ≥20-bet precondition) fits the appetite→time-budget mapping from the accumulated data and writes `mode: active` to the calibration file — that write is the switch that turns the overrun-check on.
- 90-day re-eval per §Riskiest assumption: if the pass has added digest volume WITHOUT changing outcomes on ≥3 material-change surfaces, revisit trigger or retire.

Each surface's build ticket owns the implementation contract (typed field schemas, method signatures, per-consumer routing of the dropped-vs-shipped completion predicate, test suites, cross-clone concurrency semantics). This ADR names WHAT ships, not HOW each surface implements — the how is build-tier scope per D7 / T-3439 (planning-phase-review scope-bounds work).

## Re-evaluate when

- 90 days post-landing: check whether the pass's material-change flag has fired ≥3 times with actual outcome-change on the reserved set. If not, either sharpen trigger or retire the pass (§Riskiest assumption mitigation kicks in).
- Post-T-3430 calibration (~2026-12-05 + warm-up): check whether the appetite→time-budget mapping produces false-fire noise or productive flags; adjust bands or overrun tolerance.
- Any operator-flagged instance of "the pass turned into ritual contrarianism" — immediate re-eval regardless of calendar cadence.

## Revision history

- **2026-09-05 rev-0** — Autonomous draft by Orchestrator from Specialist's ratified-in-principle proposal + appetite-scale spec. Consultant ORG-041 REBOUND (3 blockers + 3 material findings).
- **2026-09-05 rev-1** — Orchestrator-authored response addressing Consultant round-1. Grew into implementation-contract detail on some fixes.
- **2026-09-06 rev-2 (retired)** — Orchestrator-authored response addressing Consultant round-2 (4 blockers + 2 material). Grew further into impl-tier over-spec (~500 lines, method signatures, line-numbered code references, 21-step build plan). The operator steer TG 9040: "far too overgrown, revert the changes and include only things you find pass the critique bar."
- **2026-09-06 rev-3** — Reverted to rev-0 baseline (195 lines); applied targeted design-level fixes only for findings that genuinely invalidate ratifiable-decision altitude. Kept: YAML frontmatter fix, RECORD/SURFACE mode split, completeness check as distinct grooming surface, `substate: dropped` picked (not "whichever pattern"), enforcing gate with honest resolve-boundary limitation named, dedicated re-decision commands (resolve not overloaded), appetite re-scored 6→7, multi-repo transaction (not "one commit"), Codex reach narrowed to ORG-041 amendment (not universal manifest reach). Dropped: state-machine specs, method signatures, per-consumer line-numbered enumeration, 21-step build plan, appetite-ledger schema. Those belong in build-tier tickets per T-3439 (planning-phase-review scope-bounds work sibling arc). Landed at 218 lines.
- **2026-09-06 RATIFIED** — the operator via Telegram 9047 ("looks good tbh"). Doc surfaces landed same-session; code surfaces filed as build tickets per §Build plan.
