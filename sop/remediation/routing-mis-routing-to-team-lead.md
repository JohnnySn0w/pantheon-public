# Routing mis-routing to team-lead

**Status:** **Ratified 2026-06-08 by the operator.** Draft 2026-05-12 (T-0196 #7 of 9; pending the operator ratification per T-0157 framework).

## Failure shape

A dispatched worker (implementer, reviewer) routes a verdict-shaped or status-shaped artifact (`plan-READY`, `REBOUND`, `code-READY`, `RESOLVED`) to a SendMessage address that is *not* the addressed-tier's actual slot. The most common variant is **mis-routing to `team-lead`**: the brief told the worker to "address the team-lead" or "report back to team-lead," but `team-lead@<team>` is HO's slot (pre-allocated at TeamCreate time), not the project-orchestrator's slot (spawned separately, typically named `orchestrator` or auto-suffixed `team-lead-2`).

The structural cause is documented in `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md`: TeamCreate pre-allocates the `team-lead@<team_name>` slot bound to the calling agent's session-id (HO's). Subsequent `Agent` spawns with `name="team-lead"` get auto-suffixed `team-lead-2`. A brief that says "address team-lead" silently routes to HO instead of the spawned orchestrator. The worker complied with the brief literally; the brief was wrong.

Distinct from `orchestrator-tier-skip-on-approval` (authority issues verdict at wrong tier — failure is at the authority side). This SOP is at the SENDER side: the worker correctly *generated* the artifact for their dispatcher; the routing address sent it to the wrong recipient.

Variants:

- **First-fire team-lead mis-route.** Most common at TeamCreate'd cycle first-fire. b29 + b31 canonical cases. Implementer's plan-READY hits HO's inbox instead of the orchestrator's. HO forwards inline; orchestrator processes; cycle continues with the address corrected for subsequent fires.
- **Reviewer mis-route.** Reviewer's APPROVE/REBOUND verdict routes to team-lead instead of orchestrator. Same root cause; corrected the same way.
- **Cross-team-broadcast leak.** Worker addresses a SendMessage to a system address (`team-lead`) intending their own team's lead, but the address resolves at HO's session because HO holds team-lead across all teams it created. The worker's own team's orchestrator doesn't receive the artifact. Particularly insidious in multi-team scenarios.
- **Stale-address mis-route.** Brief written against an earlier team config (e.g., orchestrator named `orchestrator-v1`); team was re-spawned with different name (`orchestrator-v2`); brief wasn't updated. Worker addresses the stale name; routes to nobody or to wrong slot.
- **Persona-default mis-route.** Worker's persona brief (system prompt) carries a default address from an earlier cycle's b9 retrospective ("report back to `team-lead`"); even when the dispatch brief specifies the correct address, the persona's prior conditioning produces the system-address by default. Surfaces when worker has been used across multiple cycles.

Characteristic signal HO sees: a SendMessage hits HO's inbox carrying a plan-READY / REBOUND / code-READY / RESOLVED artifact from an implementer/reviewer inside a team that HO is NOT the active lead of. Plus, the artifact body shape is one the orchestrator should be processing (not HO directly).

## Precedent cases

- **b29 (early May 2026)** — first canonical case. Implementer's plan-READY in `b29-echo-binary-wiring` cycle mis-routed to `team-lead` (= HO) instead of orchestrator (named `orchestrator`). Per `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md`: "HO forwarded inline; orchestrator corrected for code-tier READY. Confirms Option A pattern: when orchestrator is named non-`team-lead`, the discipline holds first-fire after one inbound mis-route." Surfaced from the b9-retrospective's "use system address `team-lead`" rule being silently false when HO retains the team-lead slot AND spawns a separate orchestrator.
- **b31 (mid May 2026)** — sibling case. Same plan-tier mis-route. Per `lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`: "Same plan-tier routing mis-route as b29." b31 was the cleanest cycle of the corpus except for this routing miss + the orchestrator-tier-skip-on-approval that followed.
- **b10-kino-phase1 D1 (2026-05-05)** — first discovery of the underlying slot-collision. Documented in `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md` discovery log. Predates the b29/b31 instruction-fidelity recurrence; surfaced as a Pantheon-tier substrate bug rather than a discipline failure.
- **T-0139 transition log 2026-05-08T19:17:18Z** — explicit log entry: "implementer plan-READY (mis-routed to team-lead; HO forwarded inline)." Captured the mid-cycle remediation pattern this SOP names.

The pattern is structurally different from worker-tier discipline failures because the worker complied with the brief — the brief itself was wrong. The remediation lives primarily at the *brief-construction* tier (pre-dispatch) and at the *HO forward-inline* tier (mid-cycle recovery).

## Recommended remediation

Ranked by precedent + cost. This SOP has both a *recovery* layer (when mis-route already happened) and a *prevention* layer (how to keep it from firing again in the same cycle). Both are HO-autonomous at the recovery side; prevention happens at dispatch-time discipline.

*Terms used below.* "Team-lead slot" = `team-lead@<team_name>`, pre-allocated by TeamCreate to the calling session (HO's). "Orchestrator slot" = the actual project-orchestrator's address, typically `orchestrator@<team_name>` or auto-suffixed `team-lead-2@<team_name>`. "First-fire mis-route" = inbound mis-routed message at the cycle's first verdict-shaped artifact, before the address has been corrected for the cycle.

**Path A — HO forwards inline + brief-amendment for the live cycle.** *Default for first-occurrence; preserves the in-cycle flow without team reset.*

- HO does: (1) forward the mis-routed artifact to the correct slot via SendMessage with explicit "[forwarded from inbox; original mis-route to team-lead]" framing; (2) emit a brief-amendment SendMessage to the misrouting worker AND the orchestrator naming the correct address for subsequent fires: "Your dispatch brief said to address `team-lead`; that slot is HO's. The orchestrator's actual address is `<actual-name>@<team>`. Subsequent verdicts go there. This applies to all artifacts in this cycle."; (3) ledger entry naming the mis-route + the brief-amendment.
- Why: the worker correctly complied with the brief; the brief was wrong. The forward-inline lets the cycle continue without team reset (a tier-2 cost). The brief-amendment prevents recurrence within the cycle. The ledger entry captures the pattern for the post-cycle brief-construction discipline improvement.
- Expected outcome: cycle continues with corrected address; no further mis-routes in this cycle.
- When to escalate: if the same worker mis-routes a *second* artifact after the brief-amendment, that's no longer brief-quality (the corrected brief has been delivered); it's a worker-tier discipline failure adjacent to `claimed-prior-approval` (worker's frame is overriding the explicit correction). Apply Path B.

**Path B — Worker-only redispatch with address-readback gate.** *Use when Path A's brief-amendment doesn't stick OR when stale-address-mis-route pattern (persona-default override) is suspected.*

- HO does: orchestrator terminates current implementer/reviewer context + spawns fresh worker with brief amended to: (1) literal slot-address verbatim ("the orchestrator's address is `<actual-name>@<team>` — NOT `team-lead`"); (2) explicit readback-gate at first artifact: "Before sending your first artifact, confirm the orchestrator's address by replying to this brief with the literal address you will use. Do NOT send the artifact until that confirmation is acknowledged."
- Why: persona-default mis-route is one of the *least amenable to brief-tightening* variants — a worker conditioned by prior cycles' "use `team-lead`" defaults doesn't read a tighter brief differently; the explicit readback breaks the default by forcing an articulation step.
- Expected outcome: clean address-handling via the new context; readback-gate confirms slot-source-of-truth.
- When to escalate: if B's readback also misroutes (worker replies "I will address `team-lead`" after explicit correction), bubble up — pattern signals systemic persona-tier address-discipline regression worth a persona-brief amendment.

**Path C — Whole-team redispatch with non-`team-lead` orchestrator naming.** *Use when team-config audit shows the orchestrator was spawned with `name="team-lead"` (and got auto-suffixed `team-lead-2`); routing-discipline cannot recover cleanly because the literal address is unintuitive + every brief is at-risk of the same collision.*

- HO does: SendMessage-shutdown the current workers, then Agent-spawn a fresh cycle with orchestrator explicitly named `orchestrator` (or `<arc>-lead`, or similar non-`team-lead`). All dispatch briefs use the explicit name verbatim. The b9-retrospective's "use system address `team-lead`" rule is intentionally NOT applied; per bug `teamcreate-team-lead-slot-pre-allocation-collision.md` Option A (preferred, first-fire-correct).
- Why: when the team was spawned with the orchestrator at the colliding name (`team-lead`→`team-lead-2`), every dispatch brief that says "team-lead" is at-risk because the worker may read the literal phrase as the unsuffixed default. Whole-team redispatch with clean naming removes the collision source.
- Expected outcome: clean cycle with non-`team-lead` orchestrator naming; routing discipline holds first-fire.
- When to escalate: only if the spawn-naming convention itself is in dispute. Per the bug's Option A, non-`team-lead` naming is the established preferred pattern; Path C applies it without further ratification.

## Autonomous-vs-bubble-up D3 placement

- **Paths A, B, C: HO-autonomous.** Forward-inline + brief-amendment (Path A) is core orchestrator discipline; worker-only redispatch (Path B) and whole-team redispatch (Path C) are in `trust-posture.md` auto-approve scope. **D3 wait-time-bound: forward-inline fires immediately on detection; brief-amendment fires same-turn.**
- **D3 calibration check.** Routing-mis-routing is one of the lowest-severity SOPs in the set because the worker's variance was *brief-induced*, not discipline-tier. **Threshold: every Path-A application warrants a ledger-line note for brief-quality tracking** (so the post-cycle review can fold the pattern into brief-construction discipline). Repeated Path-A applications within a cycle warrant Path B; the trigger is *same-cycle recurrence after brief-amendment*, not a per-week count.
- **Brief-quality vs persona-quality demarcation.** Path A is brief-quality remediation (the brief was wrong; corrected mid-cycle). Path B is persona-quality remediation (the persona's prior conditioning is overriding the corrected brief). Path C is team-config remediation (the team's spawn-naming is the recurrence source). Demarcate at remediation time so post-cycle review folds the lesson into the right tier's discipline.

## Upstream prevention

This SOP is the *post-mis-route* remediation. The *pre-mis-route* discipline lives at four layers:

1. **Brief-construction tier — explicit non-`team-lead` orchestrator naming.** Per `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md` Option A: HO spawns project-orchestrator with `name="orchestrator"` (or `<arc>-lead`) at TeamCreate time, not `name="team-lead"`. This removes the slot-collision source. Codified in `sop/standard/team-orchestrator-dispatch-checklist.md` and `sop/standard/team-orchestrator-brief-construction.md`.
2. **Brief-verification step — read team config post-spawn.** Per the bug's Option C: before authoring the dispatch brief, run `cat ~/.claude/teams/<team_name>/config.json | jq '.members[].name'` to confirm actual addresses. Brief states the actual address verbatim, not the `team-lead` alias.
3. **Persona-brief default-address removal.** Implementer + reviewer persona briefs at `~/.claude/agents/` should NOT carry "address `team-lead`" as a default; should require the dispatch brief to specify the address explicitly. This is a persona-config discipline tracked by `claude-config` submodule.
4. **Harness-tier slot-collision prevention** — speculative: TeamCreate / Agent could refuse to allocate a worker into a colliding slot (return an error rather than auto-suffix). No mechanism today; candidate Anthropic feedback / harness-research per `lessons/structural-enforcement-retires-the-prose-rule.md`.

Until structural enforcement lands at layer 4, layers 1-3 are the prevention surface and this SOP fires when any of them lapse.

## Cross-references

- **Principles + directives:** P4 (traceability — routing-source is part of the trace); P5 (roles don't hop — routing-discipline is one face of role-discipline); P6 (operator comprehension — clear addresses are minimum-traversal); D4 (brief quality bar — the dispatch-time discipline this SOP backstops); D6 (plan-first protocol — plan-READY routing is the most common mis-route locus); D9 (orchestrator response contract — explicit address is part of the contract); D10 (context-frugality — slot-address verification is minimum-traversal to precise routing).
- **Bugs:** [`bugs/teamcreate-team-lead-slot-pre-allocation-collision.md`](../../bugs/teamcreate-team-lead-slot-pre-allocation-collision.md) (structural root cause; Option A/B/C remediation patterns at the substrate level — this SOP operationalizes them at discipline-tier); [`bugs/team-sendmessage-system-name-not-personal-name.md`](../../bugs/team-sendmessage-system-name-not-personal-name.md) (root-cause family — system-address-vs-personal-name routing).
- **Standard SOPs (sibling discipline):** [`../standard/team-orchestrator-dispatch-checklist.md`](../standard/team-orchestrator-dispatch-checklist.md) (pre-dispatch checklist; includes non-`team-lead` naming); [`../standard/team-orchestrator-brief-construction.md`](../standard/team-orchestrator-brief-construction.md) (brief-construction discipline).
- **Policies:** `rulebook/policies/trust-posture.md` (auto-approve scope); `rulebook/policies/autonomy.md`.
- **Lessons:** [`lessons/instruction-fidelity-collapse-discipline-vs-architecture.md`](../../lessons/instruction-fidelity-collapse-discipline-vs-architecture.md) (b29/b31 chronology + the broader corpus framing); [`lessons/structural-enforcement-retires-the-prose-rule.md`](../../lessons/structural-enforcement-retires-the-prose-rule.md) (framework for retiring this SOP's brief-language requirements once a harness primitive lands).
- **Incidents:** [`incidents/2026-05-04-b9-team-pattern-first-run/`](../../incidents/2026-05-04-b9-team-pattern-first-run/README.md) (origin of the "use system address" rule that turned out to be silently false when HO retains team-lead slot).
- **Sibling SOPs:** [`implementer-self-merge-to-main.md`](implementer-self-merge-to-main.md); [`claimed-prior-approval.md`](claimed-prior-approval.md); [`plan-first-violation.md`](plan-first-violation.md); [`iterates-against-hold.md`](iterates-against-hold.md); [`locked-answer-deviation-doubled-down.md`](locked-answer-deviation-doubled-down.md); [`orchestrator-tier-skip-on-approval.md`](orchestrator-tier-skip-on-approval.md) (adjacent pattern at the receiver's tier; this SOP is at the sender's tier).
- **Tickets:** T-0096 (b10 kerfuffle catalog; Kerfuffle 1 — TeamCreate placeholder address-collision); T-0156 (structural prevention adjacent area); T-0157 (parent ratification + this SOP framework); T-0164 (lessons umbrella); T-0196 (this ticket — 9 founding remediation SOPs).
- **Telegram refs:** msg_822 (T-0157 ratification); msg_945 (T-0196 file request via Recovery); msg_1218 + msg_1220 + msg_1223 + msg_1261 (T-0196 #1-#4 ratification cycle).
