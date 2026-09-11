# Orchestrator-tier skip on approval

**Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #6 of 9; pending the operator ratification per T-0157 framework).

## Failure shape

A tier in the dispatch chain (HO, project-orchestrator, or sub-orchestrator) gives an approval that belonged at a different tier — typically by *short-circuiting an in-tier verdict that should have been the gate*. The most common direction is **outer-tier-skipping-down**: an outer authority (HO) approves content directly that should have been routed back to the inner authority (project-orchestrator) for in-tier verdict — bypassing the chain-of-justification flow where each dispatcher↔dispatchee boundary owns its own ratifications.

The opposite direction is **inner-tier-skipping-up**: a sub-orchestrator approves work that should have routed up to HO because it crosses the autonomy bar (ADR-tier, vault-adjacent, cross-account, etc.). This is structurally rarer because most worker output stays within the dispatched-arc scope; when it doesn't, the bubble-up criteria from D7 (5 structural items) are explicit.

Distinct from `claimed-prior-approval` (worker FRAMES nonexistent approval — discipline failure at worker tier). This is the AUTHORITY actually giving an approval *at the wrong tier* — discipline failure at orchestrator/HO tier.

Distinct from `locked-answer-deviation-doubled-down` (substance + audit-chain inversion). This is a structural mis-routing of WHO is allowed to issue the approval, not the substance of what's approved.

Variants:

- **HO-tier-skips-down-on-plan-content.** Worker emits a plan READY (or DECISIONS); HO emits APPROVE on the plan content directly to the implementer instead of forwarding the plan body to the orchestrator for in-tier verdict. b31 canonical case. The orchestrator's chain-of-justification role is bypassed; their in-tier scope is now ambiguous (did HO override their authority? are subsequent in-tier verdicts still binding?).
- **HO-tier-skips-down-on-close-report.** Worker (or orchestrator) emits a close report; HO accepts directly without surfacing to whichever tier was supposed to verify acceptance (typically reviewer; sometimes the operator at ratification-tier).
- **Project-orchestrator-tier-skips-up.** Sub-orchestrator approves substantive scope-expansion or ADR-tier content that crosses the autonomy bar; should have bubbled up to HO per D7 bubble-up criteria. Per `trust-posture.md`, ADR-tier work is policy-touching and must surface; project-orch acceptance bypasses that gate.
- **Cross-lead-tier-skip.** A lead (the session-root, or a spawned project-orchestrator) receives an approval-shaped artifact intended for a different lead's lane; accepts it on behalf of their own lane without forwarding. Routing-discipline failure adjacent to `routing-mis-routing-to-team-lead` but the failure is at the receiving authority's tier, not the sender's. *(Historical TeamCreate-era framing re-expressed 2026-07-19, T-1817 — the multi-lead shape now arises via spawned project-orchestrators on the session's implicit team.)*
- **Reviewer-tier-skip-by-dispatcher.** Dispatcher (orchestrator or HO) accepts an implementer's output as DONE without firing the reviewer pre-flight. The reviewer's gate is bypassed; their in-tier scope as a non-optional discipline check is corrupted (a future reviewer dispatch sees a precedent of "sometimes dispatchers skip me; my role is advisory not load-bearing"). Sibling to plan-first-violation but the bypass is at the close-tier rather than open-tier.

Characteristic signal HO sees: a verdict-shaped artifact (APPROVE/REBOUND/RESOLVE) appears in a transcript or commit history whose dispatching-tier doesn't match the convention's expected source. For HO-tier-skips, this surfaces in HO's own retrospective scan ("did I emit an APPROVE that should have routed to the orchestrator?"). For project-orchestrator-tier-skips, this surfaces at HO's read of the dispatched cycle's transition log.

## Precedent cases

- **b29 (early May 2026)** — third instance in the b23-corpus. Same plan-tier routing mis-route as b31 (sibling). Plus a routing-tier subspecies: implementer routed plan-READY to the wrong address (parent of team-lead instead of team-lead) — that's the `routing-mis-routing-to-team-lead` adjacent pattern. The tier-skip-on-approval component: orchestrator-tier accepted the implementer's deliverable framing without verifying the verdict-source-tier match.
- **b31 (mid May 2026)** — fourth instance in the b23-corpus. **Canonical case for this SOP.** Per `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`: "HO tier-skipped APPROVE on the plan content directly to orchestrator instead of forwarding plan body for in-tier verdict — the orchestrator REBOUNDed cleanly per chain-of-justification." The orchestrator's REBOUND is the structural recovery — chain-of-justification absorbed the variance at the right tier. The SOP's existence is to make that recovery *expected* rather than incidental: HO and project-orchestrators should self-check tier-source before issuing approval-shaped artifacts.

The pattern is structurally different from the worker-tier discipline failures (`plan-first-violation`, `claimed-prior-approval`, `iterates-against-hold`) because it surfaces at an *authority* tier — the tier that issues approvals. The discipline-tier rules at that level are about *whose verdict counts at which boundary*, not about whether to comply with verdicts.

## Recommended remediation

Ranked by precedent + cost.

*Terms used below.* "Tier-source match" = the tier issuing an approval matches the tier expected by the chain-of-justification convention at that boundary (worker→orchestrator: orchestrator-tier verdict; orchestrator→HO: HO-tier verdict; HO→the operator: ratification-tier verdict; cross-team: each team-lead within their own team). "Tier-skip" = approval issued at a tier other than the convention's expected source.

**Path A — HO withdraws the tier-skip approval + forwards to in-tier authority for clean verdict.** *Default for first-occurrence; preserves the in-tier authority's role.*

- HO does: emit a literal withdrawal of the tier-skip approval ("Withdrawing the prior APPROVE I issued at HO-tier on [content]; that verdict belongs at the orchestrator tier. Forwarding the plan body to [orch-name] for in-tier verdict. Any subsequent action you took on the basis of the withdrawn approval is reversible at no cost; if you've already started, pause until the in-tier verdict returns."). Then route the content to the correct tier with explicit framing.
- Why: the in-tier authority's role is load-bearing precisely because it absorbs variance at the dispatcher↔dispatchee boundary. Tier-skipping it strips the chain-of-justification of one of its layers — the next time work flows through that boundary, the precedent "sometimes HO just decides" weakens the orchestrator's role. The withdrawal + forward is the structural reset; it costs ~one round-trip but restores the tier's load-bearing role.
- Expected outcome: in-tier authority issues their own verdict (APPROVE, REBOUND, or REQUEST_CHANGES); cycle proceeds with the chain-of-justification intact.
- When to escalate: if the in-tier authority can't issue a verdict (e.g., they've gone silent, or the work genuinely needs HO-tier ratification because it crosses the autonomy bar), apply Path B.

**Path B — Re-classify the work + route to the correct tier (rather than restoring the original tier).** *Use when the tier-skip turns out to have been at *the wrong tier altogether* — i.e., the work crosses the autonomy bar and never belonged at orchestrator-tier in the first place.*

- HO does: (1) withdraw the tier-skip approval per Path A's framing; (2) re-classify the work per the autonomy bar (ADR-tier? vault-adjacent? cross-account-coordination? cost/secret-bearing?); (3) route to the actually-correct tier (the operator for ratification; or the right cross-account ticket-queue; or whoever owns the discipline boundary that surfaces). Explicit ledger entry naming the re-classification.
- Why: tier-skips sometimes surface because the work was mis-categorized at dispatch time — the dispatcher under-classified it and the tier-skip was the "correct" routing if the under-classification had been accurate. The fix isn't to restore the under-classified path; it's to re-classify and route correctly.
- Expected outcome: work routed to the correct tier; tier-skip pattern doesn't recur for similarly-categorized future items because the classification is now correct.
- When to escalate: re-classification disputes are themselves an operator-tier call. If HO and a project-orch disagree on the work's correct tier, that disagreement is the bubble-up.

**Path C — Accept the tier-skip with explicit ledger note if reverting would cost more than the chain-of-justification damage.** *Rare; only when the operator ratifies the exception.*

- HO does: HALT before any acceptance action; surface to the operator with: (a) the tier-skip approval text; (b) the in-tier authority that was skipped; (c) the cost of withdrawal + re-route (e.g., already-committed downstream work; cross-account dispatches already fired; supervised-first-fire windows already started); (d) HO's read on whether the chain-of-justification damage from accepting is recoverable via ledger-note correction. ONLY proceed on the operator's explicit "accept + correction-note" — never autonomous.
- Why: accepting tier-skip without correction normalizes the bypass; the in-tier authority's role weakens for future cycles. Path C is the named exception; rare, ratified, never autonomous, always paired with a correction-note.
- Expected outcome: documented exception; ledger entry names the tier-skip + the correction + the exception-not-precedent marker.
- When to escalate: ALWAYS. Path C IS the escalation path.

## Autonomous-vs-bubble-up D3 placement

- **Paths A, B: HO-autonomous.** Tier-skip self-correction is core HO discipline; withdrawing + forwarding (Path A) and re-classification + re-routing (Path B) are in `trust-posture.md` auto-approve scope when applied to HO's own tier-skips or to project-orchestrators within HO's chain. **D3 wait-time-bound: withdrawal fires immediately on detection; re-routing fires within the same turn the tier-skip was observed.**
- **Path C: ALWAYS bubble-up.** Accepting tier-skip is policy-touching (would normalize bypass of in-tier authority); per D3 the autonomy bar holds.
- **D3 calibration check.** Tier-skip-on-approval is foundational because it touches the load-bearing chain-of-justification structure itself. **Threshold: HO self-application of Path A on HO's own tier-skip is a routine self-correction (no escalation); HO-application of Path A on a project-orchestrator's tier-skip warrants a same-cycle observation note in the ledger** (pattern signal at sub-orch tier). Two or more in-cycle tier-skips by the same sub-orch warrants a fresh-context redispatch per D8 sticky-belief.
- **Self-application is the most common case.** HO will catch their own tier-skips in retrospective scan ("did I just approve something that should have been forwarded to the orch?") more often than HO will catch a project-orch's tier-skip. The SOP's primary user is HO doing self-correction; the cross-tier remediation is the secondary use.

## Upstream prevention

This SOP is the *post-bypass* remediation. The *pre-bypass* discipline lives at three layers:

1. **HO-self-discipline pre-emission check** — before issuing any APPROVE/REBOUND-shaped artifact, HO asks: "is this verdict at the right tier for this artifact's source?" Prose-tier; D8 (HO self-discipline) names the general pattern but the specific tier-source check is SOP-tier guidance. Smell test: if the artifact comes from a worker a project-orchestrator dispatched, the verdict goes to that orchestrator, not the worker — even when the worker's stop-notification lands at HO's session (the completion-misroute topology; relay by reference, rule at the orchestrator's tier, per `sop/standard/dispatch-and-wait-liveness.md` §self-spawning intermediate orchestrators).
2. **Brief-language tier-source convention** — every brief carries the chain-of-justification flow explicit at the dispatcher↔dispatchee boundary (per the chain-of-justification rule in CLAUDE.md). Reviewer pre-flight scans for this section's presence.
3. **Harness-tier tier-source enforcement** — speculative: a SendMessage / TaskUpdate primitive that routes by *source-tier* rather than free-form addressing. Today's primitives let any tier send to any tier; structural enforcement of "implementer can only emit to orchestrator slot in their team" is a candidate research-dispatch. Per `lessons/structural-enforcement-retires-the-prose-rule.md`.

Until structural enforcement lands, this SOP fires every time tier-skip is observed in retrospective scan.

## Cross-references

- **Principles + directives:** P3 (decisions justify upward — the chain this SOP defends); P4 (traceability — tier-source is part of the trace); P5 (roles don't hop — the literal rule, this SOP is the remediation); D6 (plan-first protocol — plan-tier verdicts are the most common tier-skip locus); D7 (decision-flow execution + bubble-up criteria — defines when work crosses the autonomy bar, informing Path B); D3 (autonomy bar — Path C placement); D8 (HO self-discipline — pre-emission tier-source check); D10 (context-frugality — tier-source markers are minimum-traversal signals that the chain is intact).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope; tier-skip self-correction lives here); `rulebook/policies/autonomy.md`.
- **ADRs:** `architecture/adr/ORG-002` (principles/directives split — this SOP slots at policy-tier); `architecture/adr/ORG-010` (three-tier policies extension — defines the SOP-tier this file inhabits).
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (b31 + b29 chronology + the broader corpus framing); [`lessons/structural-enforcement-retires-the-prose-rule.md`](../../lessons/structural-enforcement-retires-the-prose-rule.md) (framework for retiring this SOP's brief-language requirements once a harness primitive lands).
- **Sibling SOPs:** [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md) (worker-tier structural bypass; this SOP is its authority-tier counterpart); [`claimed-prior-approval.md`](claimed-prior-approval.md) (worker FRAMES nonexistent approval; this SOP is the AUTHORITY actually giving approval at wrong tier); [`plan-first-violation.md`](plan-first-violation.md) (open-tier bypass; this SOP includes the close-tier reviewer-skip variant); [`iterates-against-hold.md`](iterates-against-hold.md); [`locked-answer-deviation-doubled-down.md`](locked-answer-deviation-doubled-down.md); [`routing-mis-routing-to-team-lead.md`](routing-mis-routing-to-team-lead.md) (adjacent pattern at the sender's tier; this SOP is at the receiver's tier).
- **Tickets:** T-0156 (structural prevention of self-merge — adjacent structural-enforcement work); T-0157 (parent ratification + this SOP framework); T-0164 (lessons umbrella covering corpus); T-0196 (this ticket — 9 founding remediation SOPs).
- **Telegram refs:** msg_822 (T-0157 ratification); msg_945 (T-0196 file request via Recovery); msg_1218 + msg_1220 + msg_1223 + msg_1261 (T-0196 #1-#4 ratification cycle); msg_1369 + msg_1367 (variance-absorption + recursive-structure framing — this SOP defends the recursive structure across dispatcher↔dispatchee boundaries).
