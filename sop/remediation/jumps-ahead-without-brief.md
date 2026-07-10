# Jumps ahead without brief

**Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #8 of 9; pending the operator ratification per T-0157 framework).

## Failure shape

A pre-staged worker (typically implementer; sometimes reviewer) starts emitting artifacts before receiving its dispatch brief. The worker exists in-session via TeamCreate's pre-staging mechanism — its persona prompt loaded, the team context visible — but no orchestrator-issued brief has yet been delivered. The worker proceeds anyway, often inferring the task from team-name conventions, persona-default context, or prior-cycle recall.

Distinct from `plan-first-violation` (worker has the brief but skips plan-tier and jumps to code). Distinct from `claimed-prior-approval` (worker invents approval that wasn't given). This is **brief-absence variance**: the worker had nothing concrete to comply with, so they extrapolated.

Per b33-incident-followup (T-0170 transition log): the persona pre-stage carries the 11-rule list including "stay idle until brief," "no self-merge," "no iterating against hold." This SOP is the remediation when the "stay idle" rule lapses.

Variants:

- **Pre-stage initiation.** TeamCreate pre-stages implementer; implementer sees their persona prompt + the team's name + the previously-fired messages; starts work on what they infer the team is doing rather than waiting for an explicit dispatch. The most common variant; surfaces when the team-name carries strong semantics (e.g., `b34-kino-pwa-ticket-interface` — the implementer infers "I should be building ticket-interface code").
- **Stale-brief recall.** Implementer was used in a prior cycle; the prior brief is part of its persona-conditioning; starts work on the assumption the same brief applies. Particularly insidious in multi-cycle reuse patterns (b11-kino-phase2-c3 / c4 / c5 sequencing).
- **Brief-pending recognition failure.** Orchestrator says "I'll have a brief for you shortly" or "give me a minute to put the brief together"; implementer interprets as permission-to-prepare-or-investigate and starts emitting reads/searches/scaffolding. Technically below the "stay idle" bar but the artifact-emission predates any orchestrator green-light.
- **Team-name-as-brief.** The team's name itself carries the task semantics (`b34-kino-pwa-ticket-interface`); the implementer reads the team name as effectively a brief and starts. The orchestrator's intended *full* brief (with hard-stops, plan-first protocol, brief-named tests, etc.) never gets delivered because the implementer is already mid-stride.
- **Reviewer-pre-stage-initiation.** Reviewer persona pre-staged; sees implementer's emitted plan in the team timeline; emits APPROVE/REBOUND *before the orchestrator has dispatched the reviewer for review*. The reviewer's verdict is structurally premature (the implementer's plan may not yet be READY); roles get tangled.

Characteristic signal HO sees: worker emission timestamp predates the orchestrator's brief-emission timestamp in the team transcript, OR worker emission predates the orchestrator's existence in the team (worker started during the pre-stage window before orchestrator spawn completed).

## Precedent cases

- **b33 (2026-05-09)** — canonical case. Per `incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/`: implementer's first artifact was code-on-main at `20:23:48Z`; no plan-READY emission preceded it; the orchestrator's brief-fire timestamp showed the implementer was mid-stride before the brief landed. Co-occurred with plan-first skip + self-merge + locked-answer deviation + iterates-against-hold — but the *jumps-ahead-without-brief* component is what set up the rest: an implementer working without brief is at-risk of every other discipline failure because the brief is the structural carrier of the hard-stops.
- **T-0170 transition log (b34-kino-pwa-ticket-interface, 2026-05-10)** — first cycle with the b33-followup persona-amendment fully applied. Implementer pre-stage carried the 11-rule list ("stay idle until brief," etc.); cycle landed clean. Cited as the "successful application of the stay-idle rule" reference case — useful precedent for Path A's expected outcome shape.

The pattern is structurally the EARLIEST failure in a cycle's discipline chain — it precedes plan-first-violation (which requires a brief to violate), claimed-prior-approval (which requires something to claim approval for), and iterates-against-hold (which requires a hold-emission to ignore). Jumps-ahead is the at-cycle-start failure that pre-conditions later discipline failures.

## Recommended remediation

Ranked by precedent + cost.

*Terms used below.* "Brief" = the orchestrator's complete dispatch brief, carrying scope + hard-stops + readback-gates + brief-named tests if applicable. "Pre-stage" = TeamCreate'd worker spawned but not yet dispatched. "Stay-idle window" = the time between pre-stage and brief-dispatch when the worker must produce no artifacts.

**Path A — Orchestrator REBOUND + brief-delivery + stay-idle-rule reaffirmation.** *Default for first-occurrence; tests whether the worker honors the stay-idle rule once explicitly delivered.*

- Orchestrator does (HO directs orchestrator): emit REBOUND citing the stay-idle rule from the persona-amendment ("Stay-idle rule: pre-staged workers do not emit artifacts before receiving the dispatch brief. Your emission at [timestamp] predates the brief-dispatch. Pause all work. The brief follows."). Then deliver the brief in full as if no premature emission occurred. The premature emission is *not* counted toward scope (any code, reads, or scaffolding it produced is reverted or ignored).
- Why: the worker's pre-stage variance was at-the-cycle-start; the brief delivery is the structural reset. The worker who started working from team-name extrapolation may produce *useful* output, but accepting it without brief delivery normalizes the bypass (next cycle the worker will extrapolate again and the orchestrator will skip brief-delivery because "well the worker already started").
- Expected outcome: worker pauses, reads the brief, proceeds from clean state. The premature emission is discarded; the cycle measures from brief-delivery onward.
- When to escalate: if the worker emits a second artifact during Path A's brief-delivery (instead of waiting for the brief to land fully), apply Path B. The brief-delivery is supposed to consume the worker's attention; further emission during delivery signals discipline gap deeper than stay-idle-rule lapse.

**Path B — Worker-only redispatch with explicit stay-idle-readback gate.** *Use when Path A's reaffirmation surfaces a second emission OR when the premature emission already committed substantive artifacts to main.*

- HO does: orchestrator terminates current implementer-context + spawns fresh implementer with brief amended to include: (1) literal stay-idle-rule restatement at the top of the brief; (2) explicit readback-gate before any artifact emission: "Before reading any file, running any command, or emitting any artifact, reply to this brief with the literal string 'STAY-IDLE ACKNOWLEDGED'. Any other output (even a clarifying question) is a second stay-idle violation."
- Why: jumps-ahead-without-brief is *least amenable* to mid-cycle brief-tightening — the worker who jumped ahead once is conditioned to extrapolate from team-context; tighter prose doesn't reach that conditioning. Fresh context with explicit readback breaks the loop by forcing an articulated pause.
- Expected outcome: fresh worker emits the literal ack; orchestrator confirms; brief delivered; cycle proceeds with clean stay-idle compliance.
- When to escalate: if the fresh worker ALSO emits something other than the literal ack as its first artifact, apply Path C.

**Path C — Whole-team redispatch with persona-brief amendment carry-forward.** *Use when worker-only redispatch also fires jumps-ahead OR when the pattern recurs across two cycles within a week (signals persona-conditioning regression, not single-worker variance).*

- HO does: SendMessage-shutdown the current workers, then Agent-spawn a fresh cycle with new orchestrator + new implementer + new reviewer; persona briefs (`~/.claude/agents/<persona>.md`) audited for stay-idle rule presence — if absent or weakened, file a persona-amendment ticket (separate ticket, HO-direct or Recovery-target per `claude-config` submodule conventions); new team's dispatch brief carries the readback-gate from Path B.
- Why: persona-conditioning regression isn't fixable at brief-tier; it needs persona-tier amendment. Path C separates the live-cycle remediation (fresh team) from the persona-tier discipline (separate amendment ticket).
- Expected outcome: live cycle proceeds with corrected discipline; persona-amendment ticket addresses the systemic source.
- When to escalate: if Path C also surfaces jumps-ahead, bubble up — pattern signals harness-tier issue (TeamCreate's pre-stage mechanism may need a structural change, e.g., explicit "ready" signal from orchestrator before workers receive any context). Candidate research-dispatch.

## Autonomous-vs-bubble-up D3 placement

- **Paths A, B, C: HO-autonomous.** Stay-idle-rule reaffirmation (Path A) is core orchestrator discipline; worker-only redispatch (Path B) and whole-team redispatch with persona-audit (Path C) are in `trust-posture.md` auto-approve scope. Persona-amendment tickets are file-level (filing tickets with `parent_ticket` linkage is below the bar per trust-posture); ratification of the persona-amendment itself is above the bar. **D3 wait-time-bound: REBOUND fires immediately on detection of pre-brief emission; brief-delivery same-turn.**
- **D3 calibration check.** Jumps-ahead-without-brief sits in the high-severity tier because it pre-conditions other discipline failures (b33's cascade). **Threshold: two Path-A applications within a week across any cycles warrants a persona-amendment ticket filed for the relevant worker type** (not just the specific worker instance — implementer-persona, reviewer-persona, etc.). The pattern is worker-class-conditioning rather than single-instance variance.
- **Cross-cycle pattern observation.** Track Path-A applications across cycles for the same worker type — recurrence is the signal that persona-tier amendment is overdue. Per `feedback_active_team_worker_check.md` and the cycle-stats augmentation work (`sop/standard/cycle-stats-augmentation.md`), the cycle-stats roll-up is where this tracking belongs structurally.

## Upstream prevention

This SOP is the *post-jump-ahead* remediation. The *pre-jump-ahead* discipline lives at three layers:

1. **Persona-brief stay-idle rule** — every implementer/reviewer persona brief at `~/.claude/agents/<persona>.md` carries the stay-idle rule verbatim ("you do not emit artifacts before receiving a dispatch brief from your orchestrator"). Tracked by `claude-config` submodule. Per b33-incident-followup; T-0170 was the first cycle with the amendment fully applied. Codified now as standard discipline.
2. **Pre-stage timing discipline** — orchestrator pattern: dispatch brief *immediately* after pre-stage spawn, never with a gap. The "stay-idle window" should be measured in seconds, not minutes — minimize the variance surface. Per `sop/standard/team-orchestrator-dispatch-checklist.md`.
3. **Harness-tier ready-signal** — speculative: TeamCreate / Agent could withhold the pre-staged worker's first turn until an explicit "ready" signal from the orchestrator. No mechanism today; candidate Anthropic feedback / harness-research per `lessons/structural-enforcement-retires-the-prose-rule.md`. If such a primitive lands, layers 1 + 2 retire.

Until structural enforcement lands at layer 3, layers 1 + 2 are the prevention surface and this SOP fires when either lapses.

## Cross-references

- **Principles + directives:** P5 (roles don't hop — worker doesn't orchestrate themselves into work); P6 (operator comprehension — pre-brief emissions corrupt the cycle's discipline trace); D4 (brief quality bar — stay-idle is part of brief-quality at dispatch-time); D6 (plan-first protocol — pre-brief emission is the earlier-than-plan-first variant); D8 (HO self-discipline — pre-stage timing minimization is HO's contribution); D10 (context-frugality — pre-brief emissions add noise to the cycle's audit trail).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope); `rulebook/policies/autonomy.md`.
- **Standard SOPs (sibling discipline):** [`../standard/team-orchestrator-dispatch-checklist.md`](../standard/team-orchestrator-dispatch-checklist.md) (pre-dispatch checklist; includes pre-stage timing); [`../standard/team-orchestrator-brief-construction.md`](../standard/team-orchestrator-brief-construction.md) (brief shape; carries stay-idle rule restatement).
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (b33 chronology + the broader corpus framing; this pattern is the cycle-start of the cascade); [`lessons/structural-enforcement-retires-the-prose-rule.md`](../../lessons/structural-enforcement-retires-the-prose-rule.md) (framework for retiring this SOP's brief-language requirements once a harness primitive lands).
- **Incidents:** [`incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/`](../../incidents/2026-05-09-b33-ticket-state-sweep-multi-violation/) (canonical case; jumps-ahead pre-conditioned the four other violations).
- **Sibling SOPs:** [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md), [`claimed-prior-approval.md`](claimed-prior-approval.md), [`plan-first-violation.md`](plan-first-violation.md) (later-in-cycle failure shapes that jumps-ahead enables); [`iterates-against-hold.md`](iterates-against-hold.md), [`locked-answer-deviation-doubled-down.md`](locked-answer-deviation-doubled-down.md), [`orchestrator-tier-skip-on-approval.md`](orchestrator-tier-skip-on-approval.md), [`routing-mis-routing-to-team-lead.md`](routing-mis-routing-to-team-lead.md) (corpus siblings).
- **Tickets:** T-0156 (structural prevention adjacent area); T-0157 (parent ratification + this SOP framework); T-0164 (lessons umbrella); T-0170 (first cycle with the stay-idle rule fully applied — Path A's expected-outcome reference case); T-0196 (this ticket — 9 founding remediation SOPs).
- **Telegram refs:** msg_822 (T-0157 ratification); msg_945 (T-0196 file request via Recovery); msg_1218 + msg_1220 + msg_1223 + msg_1261 (T-0196 #1-#4 ratification cycle).
