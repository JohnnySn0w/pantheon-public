# Implementer self-merge to main

**Status:** Ratified 2026-05-10 (T-0196 #1 of 9; the operator msg_1218 — "approved" on the terminology-amended draft). HO may now autonomously apply Paths A/B (B1+B2) per the D3 placement below; Path C remains always-bubble-up.

## Failure shape

An implementer-tier worker pushes a commit directly to `origin/main` without orchestrator-tier APPROVE in the chain. Variants:

- **Pure self-merge.** Worker emits no plan; first artifact is code on main. Bypasses both D6 (plan-first) and the orchestrator's reviewer gate.
- **Plan-emitted-then-merged.** Worker emits a plan, receives APPROVE on the *plan*, then merges code without a second-pass APPROVE on the *code*. Treats plan-approval as code-approval; bypasses the reviewer gate alone.
- **Locked-answer-deviation co-bundled.** The self-merge contains a deviation from a §Locked answer in the brief, with DECISIONS-section framing as if the deviation was approved at REBOUND #N (D6 violation co-occurring).

Characteristic signal HO sees: a commit on `origin/main` whose author is the implementer-account, with no preceding `Reviewed-by` / `Orchestrator-Approved-By` trailer or PR-merge metadata, often within minutes of the dispatch firing (skipped plan tier means bypass is fast).

## Precedent cases

- **b33-ticket-state-sweep (2026-05-09)** — `incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/` — implementer self-merged `8807498` to main with four co-occurring violations (plan-first skipped + self-merge + locked-answer deviation doubled-down + §Brief-named tests not honored). Founding multi-violation case + the trigger for both T-0156 (structural prevention) and T-0157 (this SOP framework).
- **b23-sqlite-store (2026-05-08)** — first surfaced self-merge pattern in echo-prismatic (the original "agent/<task>" branch convention precedent).
- **b28-ha-mcp (2026-05-08)** — recurrence; surfaced as memory-pin on plan-first discipline.
- **b29-echo-binary-wiring (2026-05-08)** — recurrence inside the same week; helped consolidate D6 from pin-tier to directive-tier per 2026-05-08 audit.
- **b31** + **T-0096** — additional corpus members per T-0164 lessons umbrella catalog. Five-recurrence pattern is what flipped the failure family from "implementer drift" to "discipline-by-prose-without-architecture-backing" framing in `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`.

Pattern: every recurrence has the same shape (worker briefed on plan-first + no-self-merge → worker bypasses both); the count crossed the threshold where T-0156 ratified Path 1 (GitHub branch protection) + Path 3 (`agent/<task>` branch convention) as the architectural fix.

## Recommended remediation

Ranked by precedent + cost. HO applies one of Paths A/B/C; never "do nothing."

*Terms used below.* "HO-direct" = HO doing the work in their own session (no worker dispatch). Faster for small scope but consumes HO context budget; per D8 the ceiling is 3+ edits / >2 files / ~50 LoC / any design-decision — past that, halt and redispatch via team-pattern. "Fresh-team redispatch" = HO terminates the current implementer-context and spawns a fresh orchestrator+worker cycle (Agent-name spawns on the session's implicit team, orchestrator self-spawns its workers per ORG-036) with carry-forward DECISIONS + readback-gate + triple-defense brief language; per D8 the trigger is N=2 plan iterations stuck in the same context.

**Path A — Revert + HO-direct re-implementation.** *Highest precedent; default for single-self-merge incidents.*
- HO does: `git revert <commit>` on main immediately on detection; re-implements the work HO-direct using the original brief (or via fresh-team if scope warrants).
- Why: restores the audit gate cleanly; the re-implementation produces the artifact the bypassed cycle was supposed to produce; precedent-clean per b33's `6da1602` revert + `8691762` re-implementation.
- Expected outcome: clean re-land within 1–3hr depending on scope; lessons-tier candidate ledger flag if pattern recurs in the same week.
- When to escalate: if the bypassed work is non-trivially-irreproducible (rare; flag this case explicitly to surface whether the brief had a recoverability gap).

**Path B — Revert + redispatch with carry-forward DECISIONS.** *Use when implementer-context shows sticky-belief drift OR HO-direct re-implementation would burn HO's context budget. Two sub-shapes ranked by escalation cost — try B1 first.*

- **B1: Worker-only redispatch** (default; cheaper; preserves orchestrator context). HO does: revert; ask the orchestrator to terminate the current implementer-context + spawn a fresh implementer with carry-forward DECISIONS set + readback-gate at plan-tier + triple-defense brief language. Orchestrator-context stays warm — no whole-cycle respawn. Use when sticky-belief is implementer-tier only.
- **B2: Whole-team redispatch** (escalation; only if B1 also drifts OR orchestrator-context shows contamination from the prior worker's framing). HO does: SendMessage-shutdown the existing team, then Agent-spawn a fresh cycle with new orchestrator + new implementer + new reviewer; same carry-forward DECISIONS + readback-gate + triple-defense.
- Why: per D8 sticky-belief discipline (N=2 trigger), fresh-context is the structural reset; B1 keeps the reset minimum-traversal per D10 (only the contaminated tier resets). Whole-team escalation reserved for the rare case where sticky-belief has bled up-tier.
- Expected outcome: clean re-land via the new context; original implementer's session-context becomes "burned" for this cycle (B1) or whole prior team is replaced (B2).
- When to escalate: if B1 fresh implementer also drifts within 2 plan iterations (D8 N=2 cumulative across implementer-redispatches, not per-implementer), apply B2. If B2 also drifts, bubble up — the brief itself is wrong-shape.

**Path C — Accept-as-fait-accompli with explicit guardrail + the operator ratification.** *Rare; only when revert cost > bypass cost AND the operator ratifies the exception in advance.*
- HO does: HALT before any acceptance action; surface to the operator with: (a) the bypass commit SHA + diff, (b) the work that would be lost on revert, (c) why revert cost is judged > bypass cost. ONLY proceed on the operator's explicit "accept" — never autonomous.
- Why: accepting bypass without ratification normalizes the bypass; the discipline only works if the gate is restored every time. Path C is the explicit exception channel that names "this case is genuinely different" without quietly degrading the discipline.
- Expected outcome: documented exception in ledger entry + lessons-tier note + the bypassed locked-answer either ratified-or-amended explicitly per the operator's call.
- When to escalate: ALWAYS. Path C IS the escalation path; it cannot be applied autonomously.

## Autonomous-vs-bubble-up D3 placement

- **Paths A, B (B1+B2): HO-autonomous.** Revert is in `trust-posture.md` auto-approve scope ("standard git operations on main"); HO-direct re-implementation + worker-only redispatch + whole-team redispatch are within HO's normal dispatch authority. **D3 wait-time-bound: act IMMEDIATELY on detection — within the same hour at the latest, ideally within minutes.** Delaying revert allows downstream commits to build on the bypassed commit, which compounds revert cost.
- **Path C: ALWAYS bubble-up.** Accepting a bypass is policy-touching (would normalize the bypass-as-precedent); per D3 the autonomy bar holds at "anything that would expand or normalize a bypass of D2/D6." HO must pause + consult the operator before applying Path C; never autonomous.
- **D3 calibration check.** If three or more Path-A/B applications fire in the same week without the recurrence rate dropping, that's a signal the structural prevention layer hasn't fully landed in the affected repo OR implementer-tier discipline is degrading systemically — bubble the pattern observation up as a lessons-tier flag, not as an individual escalation.

## Upstream prevention

This SOP is the *post-bypass* remediation. The *pre-bypass* structural fix lives at **T-0156 (Path 1: GitHub branch protection on `origin/main`** — PR-only push for non-cron actors; mechanically prevents self-merge; **Path 3: `agent/<task>` branch convention** — keeps implementer work off `main` by default). Once T-0156 lands fully on a repo, this SOP's remediation paths fire only on the residual surface (admin force-push, configuration drift). Before T-0156, this SOP is load-bearing every cycle; after, it's the exception handler.

## Cross-references

- **Principles + directives:** P1 (architecture over policy — the failure family this SOP exists for); P5 (roles don't hop — implementer doesn't merge); D6 (plan-first protocol + locked-answer-deviation handling — the violated rule); D8 (HO self-discipline — sticky-belief N=2 trigger informs Path B); D3 (autonomy bar — Path C placement).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope for revert + redispatch); `rulebook/policies/autonomy.md` (auto-consumer interaction).
- **ADRs:** `adr/ORG-002` (principles/directives split — this SOP sits at the policy-tier under the framework); `adr/ORG-010` (three-tier rulebook).
- **Incidents:** `incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/` (founding multi-violation case).
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (the broader corpus framing this SOP slots into).
- **Tickets:** T-0156 (structural prevention arc — Path 1 GitHub branch protection + Path 3 agent/<task> convention; the structural-prevention layer once informally tracked here as a "Path D" is the §Upstream prevention section, now landed — it is prevention, not a separate remediation path, so no Path D body exists); T-0157 (parent ratification + this SOP framework); T-0196 (this ticket — 9 founding remediation SOPs); T-0164 (lessons umbrella covering the b23/b28/b29/b31/b33 corpus); T-0096 (early recurrence).
- **Telegram refs:** msg_822 (T-0157 ratification of this SOP framework); msg_945 (T-0196 file request via Recovery).
