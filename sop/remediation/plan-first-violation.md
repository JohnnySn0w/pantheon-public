# Plan-first violation

**Status:** Ratified 2026-05-10 (T-0196 #3 of 9; the operator msg_1218 + msg_1220 + msg_1223 critique → e5a34ea amendments → msg_1236 "with the amendments, 196 and 197 should be fine"). HO may now autonomously apply Paths A/B1/B2 per the D3 placement; Path C remains always-bubble-up.

## Failure shape

A worker emits code (commit, file write, branch push, edit) before emitting a plan-tier surface that the orchestrator can REBOUND-or-APPROVE on. Variants:

- **Pure plan-first skip.** No plan emitted at any point; first artifact is code. Direct D6 violation.
- **Post-code plan backfill.** Worker emits code, then emits a plan retrospectively to "match" the code. The plan is descriptive rather than design-intent; the audit chain shows code-then-plan rather than plan-then-code.
- **Description-as-plan.** Plan-tier emission is a description of what the code will do, not the design intent + decisions. Lacks DECISIONS section, lacks alternatives-considered, lacks justifies-via P/D citations. Effectively a code-walkthrough with "plan" in the heading.
- **Self-approved plan.** Plan emitted; worker immediately self-approves ("plan looks good, proceeding to code") without orchestrator/HO REBOUND-or-APPROVE in the chain. Bypasses the orchestrator-tier gate even though the plan-tier surface exists.

Characteristic signal HO sees: at close-report review, the dispatch transcript shows code-emission preceding plan-emission (or no plan at all), or the "plan" is descriptive rather than decisional. Per D6: "implementers return a PLAN before writing code; orchestrator green-lights or REBOUNDs before any edit lands."

## Precedent cases

- **b33-ticket-state-sweep (2026-05-09)** — `incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/` — pure plan-first skip co-occurring with self-merge + locked-answer deviation + §Brief-named tests not honored. **First listed of the four co-occurring violations:** "No plan emitted; first artifact was code on main (D6 violation)." Founding case.
- **b23-sqlite-store (2026-05-08)** — original plan-first skip surface in echo-prismatic; one of the founding pins for D6 consolidation per 2026-05-08 audit.
- **b28-ha-mcp (2026-05-08)** — plan-first skip recurrence; second pin contributing to D6 consolidation.
- **b29-echo-binary-wiring (2026-05-08)** — recurrence; helped lock D6 from pin-tier to directive-tier.
- **b31, T-0096** — additional corpus members per T-0164 lessons umbrella catalog.

The pattern is the *most-recurring* of the b23-corpus failures. T-0156 Path 1 (GitHub branch protection) is the structural prevention layer — it blocks the code-emission-to-main step that follows the plan-first skip; this SOP handles the post-bypass remediation when the structural fix isn't fully landed in the affected repo.

## Recommended remediation

Ranked by precedent + cost.

*Terms used below.* "Plan-tier surface" = the structured plan emission with §Locked answers + §Plan + §Tests + §DECISIONS sections (per the brief-template SOP). "REBOUND" = orchestrator/HO reply in the dispatch transcript that names the gap + asks for re-emission; counts against the 3-REBOUND budget per cycle.

**Path A — REBOUND with "discard code; emit plan; await APPROVE."** *Default for first-occurrence; cheapest correction.*
- HO does: REBOUND the cycle with explicit ask: "Code emitted before plan-tier surface. Discard code (revert if pushed); emit a proper plan-tier surface with §Locked / §Plan / §Tests / §DECISIONS-with-justifies-via; await APPROVE before any further code." If code was pushed to main, also fire `git revert` per `implementer-self-merge-to-main.md` Path A.
- Why: plan-first is an audit-chain discipline; restoring the gate retroactively requires discarding the bypassed-from artifact + re-running through the gate cleanly. The "discard code" instruction is structural — without it, the worker may try to keep the code + emit a backfill plan (post-code plan backfill variant).
- Expected outcome: worker emits proper plan-tier surface; orchestrator REBOUNDs or APPROVEs the plan; clean code-tier follows. Lessons-tier flag if pattern recurs in the same week.
- When to escalate: if the worker emits another code-first artifact in response (or a backfill-plan that's actually descriptive), apply Path B.

**Path B — Revert + redispatch with carry-forward DECISIONS.** *Use when Path A's REBOUND surfaces continued plan-first skip OR when the original cycle co-occurs with locked-answer deviation. Two sub-shapes ranked by escalation cost — try B1 first.*

- **B1: Worker-only redispatch** (default; preserves orchestrator context). HO does: revert any pushed code; ask the orchestrator to terminate the current implementer-context + spawn a fresh implementer with brief-amended hard-stop language ("HARD STOP: emit plan-tier surface as your FIRST artifact; if you emit any code before plan APPROVE, the cycle escalates to HO REBOUND regardless of code quality") + readback-gate at plan-tier. Use when plan-first skip is implementer-tier only.
- **B2: Whole-team redispatch** (escalation; if B1 also drifts OR orchestrator-context absorbed the skip via accepting code-first emissions). HO does: SendMessage-shutdown the current workers, then Agent-spawn a fresh cycle with new orchestrator + new implementer + new reviewer; same hard-stop brief amendment + triple-defense + readback-gate.
- Why: per D8 sticky-belief discipline (N=2 trigger), fresh-context is the structural reset. B1 keeps the reset minimum-traversal per D10. The hard-stop brief amendment is the structural fix — a worker briefed with explicit "first artifact must be plan" language has fewer bypass surfaces.
- Expected outcome: clean re-land via the new context with proper plan-tier surface; original worker's session-context burned (B1) or whole prior team replaced (B2).
- When to escalate: if B1 fresh implementer also skips plan-tier, apply B2. If B2 also skips, bubble up — the brief-template itself may have a clarity gap.

**Path C — Accept-with-plan-backfill if code is materially correct + revert cost > backfill cost.** *Rare; only when the operator ratifies the exception.*
- HO does: HALT before any acceptance action; surface to the operator with: (a) the code emission + diff, (b) why a proper plan would have arrived at the same code, (c) why revert + re-plan cost > accept + retroactive-plan cost. ONLY proceed on the operator's explicit "accept + backfill" — never autonomous.
- Why: plan-first is the discipline that keeps the audit chain auditable; accepting code-first emissions normalizes the bypass + degrades the audit chain. Path C is the exception channel for the rare case where the bypass is one-off + correct on merit + the structural fix already landed (so the recurrence won't compound).
- Expected outcome: documented exception; the retroactive plan goes in the ticket transition log + ledger entry; the bypass is not cited as autonomously-applicable precedent.
- When to escalate: ALWAYS. Path C IS the escalation path; never autonomous.

## Autonomous-vs-bubble-up D3 placement

- **Paths A, B (B1+B2): HO-autonomous.** REBOUND + revert + worker-only/whole-team redispatch are all in `trust-posture.md` auto-approve scope. **D3 wait-time-bound: REBOUND fires immediately on detection; revert immediately if code was pushed.**
- **Path C: ALWAYS bubble-up.** Accepting code-first emission with retroactive plan is policy-touching (would normalize the bypass); per D3 the autonomy bar holds.
- **D3 calibration check.** If three or more Path-A/B applications fire in the same week without the recurrence rate dropping, the structural prevention layer may not be fully landed in the affected repo OR the brief-template may have a regression — bubble the pattern observation up as a lessons-tier flag.

## Upstream prevention

This SOP is the *post-bypass* remediation. The *pre-bypass* structural fix is **T-0156 Path 1 (GitHub branch protection)** + **Path 3 (`agent/<task>` branch convention)** — together they collapse the code-emission-to-main step that follows plan-first skip. The orthogonal *pre-bypass discipline* is the **brief-template hard-stop language at dispatch time** (see [`../standard/plan-first-dispatch.md`](../standard/plan-first-dispatch.md) for the prescriptive standard). When both layers are in place on a repo, this SOP fires only on the residual surface: pure discipline-drift bypassing both the structure and the brief-language. Before T-0156 lands, this SOP is load-bearing every cycle.

## Cross-references

- **Principles + directives:** P1 (architecture over policy — failure family); P5 (roles don't hop — implementer doesn't ratify own plan); D6 (plan-first protocol + locked-answer-deviation handling — directly violated rule); D8 (HO self-discipline — sticky-belief N=2 trigger informs Path B); D3 (autonomy bar — Path C placement).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope for REBOUND + revert + redispatch); `rulebook/policies/autonomy.md`.
- **ADRs:** `architecture/adr/ORG-002` (principles/directives split — this SOP slots at the policy-tier).
- **Incidents:** `incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/` (founding multi-violation case; plan-first skip listed first of four).
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (broader corpus framing).
- **Sibling SOPs:** [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md) (often co-occurs with plan-first skip; revert mechanics shared); [`claimed-prior-approval.md`](claimed-prior-approval.md) (sibling — framing-collapse often follows from plan-first skip when worker tries to back-attribute approval).
- **Standard-bucket sibling:** [`../standard/plan-first-dispatch.md`](../standard/plan-first-dispatch.md) — the *prescriptive* counterpart to this remediation SOP; the discipline whose violation this SOP responds to.
- **Tickets:** T-0156 (structural prevention via branch protection + agent/<task> convention); T-0157 (parent ratification + this SOP framework); T-0164 (lessons umbrella covering corpus); T-0196 (this ticket — 9 founding remediation SOPs); T-0096 (early recurrence).
- **Telegram refs:** msg_822 (T-0157 ratification); msg_945 (T-0196 file request via Recovery); msg_1218 + msg_1220 (T-0196 #1 + #2 ratification — sibling SOP precedent for this draft's shape including Path B split).
