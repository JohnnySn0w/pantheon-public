---
aliases: [ORG-037]
title: Team-coordination liveness — turn-firing watchdog + bilateral pending-action self-checks
status: Ratified 2026-06-26 by the operator (Layer 3 actuation gates on the operator)
date: 2026-06-25
author: Orchestrator (Orchestrator)
ticket: T-1315
supersedes-partial: ORG-011 P1.c (backed out T-0228) — reopens a NARROWER form with new evidence
build_disposition: built
---
# ORG-037 — Team-coordination liveness

**Status:** **Ratified 2026-06-26 by the operator.** Authored by HO under ORG-012 (HO is primary drafter of `adr/`). The design (Layers 1–2 + the process lesson) is within HO auto-approve scope and the implementation SOP landed on ratification; **Layer 3 (a Recovery-side backstop) reopens the P1.c-shaped cross-account surface that msg_1326 closed, so its actuation specifically gates on the operator.** Motivating incident: `incidents/2026-06-25-orchestrator-stall-dropped-wake/`.

## Context

The team-pattern wake-hook is intermittently unreliable (`bugs/team-pattern-sendmessage-intermittent-wake-failure.md`): worker→orchestrator and CI-completion wakes drop; HO→worker SendMessage *from outside the team* and task-notifications *to the spawner* are reliable.

ORG-011 enforces HO-stays-working-under-silence with a Recovery-side activity-monitor:
- **P1.a** (idle-with-actionable-queue) and **P1.b** (operator-absent-with-pullable-work) — **landed**, cover the *HO-absent + queue-existing* surface.
- **P1.c** (worker-silent-while-cycle-open, a Recovery-side per-role-threshold predicate) — **backed out** (T-0228) on the operator's msg_1326 verdict: overengineered, "no legitimate HO-absent + team-active state to detect," the discipline-tier active-team smell-test is the right shape.

The **2026-06-25 incident** is empirical proof that msg_1326's reasoning had a hole. It was right that there is no HO-*absent* + team-active state. It missed the **HO-*present-but-waiting* + dispatchee-silent + completion-wake-dropped** state: the reviewer APPROVE'd B1, that wake routed to the orchestrator and dropped, the orchestrator never resumed — and **nothing re-invoked HO either**, so HO's end-of-turn active-team smell-test never ran. ~4h zero progress, surfaced only by the operator's unrelated "how are things?".

Generalized per the operator (TG 4154) from the original HO↔orchestrator scope: the gap is structural to the team primitive and applies at **every** dispatcher↔dispatchee boundary — HO↔orchestrator, orchestrator↔implementer, orchestrator↔reviewer, HO↔researcher.

## The load-bearing finding

Every discipline-tier self-check — the dispatcher's active-team worker-silence smell-test, and the dispatchee's pending-action ("APPROVE'd-but-unmerged") self-check — runs **only at end-of-turn, i.e. only when a turn fires.** The failure mode is *precisely* that the wake which would fire a turn dropped. Therefore **no arrangement of discipline-tier checks can close the gap**: a turn-firing mechanism *independent of the flaky wake* is structurally required.

This also reconciles msg_1326: the "smell-test covers it" verdict was correct for the HO-*absent* case (Recovery's P1.a/P1.b fire a turn externally) and wrong for the dispatcher-*waiting* case (in which there is, by construction, no turn for the smell-test to run in). The two cases need different mechanisms; conflating them is what left the quadrant uncovered.

## Forces

- **D1 (the operator's bandwidth)** — a silent stall is the worst property: it surfaces only when the operator asks "how are things?". Every such ask is a withdrawal.
- **P1 (architecture over policy)** — the turn-firer must be structural (a harness primitive), not prose ("remember to check").
- **D10 (context-frugality)** — too-frequent self-wakeups burn budget on no-op cycles; cadence is bounded (≥1200s fallback) and re-arms only while an await is open.
- **P6** — preserve the catch-fast ↔ burn-budget tension; calibrate cadence empirically.
- **The msg_1326 constraint** — do not re-introduce a broad Recovery-side worker-silence monitor. Any Recovery surface must be strictly narrower and gated on the operator.

## Decision — three layers

### Layer 1 (load-bearing — turn-firing): dispatcher-side self-wakeup while holding an in-flight dispatch

When any agent dispatches a teammate and enters an await-state, it **arms a periodic self-wakeup** that re-fires its own turn on a bounded cadence. On each fire it **ground-truths arc progress** (dispatchee inbox-mtime + repo/PR/ticket state vs expected) and either (re-poke the dispatchee + re-arm) or (the await resolved → proceed). It **re-arms until the await resolves.**

At the HO/session tier this is the `ScheduleWakeup` harness primitive (the exact stopgap HO ran during the incident recovery, armed 22:52Z, which caught the *second* stall). It is independent of the team wake-hook, so it fires the turn the discipline-tier checks need.

**Why this is NOT rejected-Path-3.** ORG-011 rejected `ScheduleWakeup` as the primary *HO-drift* mechanism because "it depends on HO remembering [every turn]" — and HO-drift has no specific trigger moment. The dispatcher-waiting case is different: the trigger is **single and natural** — *arm-when-you-dispatch-and-wait*, bound to the dispatch ritual, and it re-arms itself thereafter. The "remember" cost is one action at one well-defined moment, not a per-turn discipline. Different failure surface → different correct mechanism.

### Layer 2 (defense-in-depth — what to DO when a turn fires): bilateral discipline self-checks

- **Dispatcher half:** on every turn (normal end-of-turn AND a Layer-1 watchdog fire), if holding an in-flight dispatch, ground-truth the dispatchee against its role-threshold (orchestrator 30 min / implementer 15 min / reviewer 5 min — the P1.c numbers were sound, kept) and status-check via the **reliable** direction (spawner→subagent SendMessage; task-notification-to-spawner) if stale.
- **Dispatchee half:** on every end-of-turn, self-check *"do I hold a completed-but-un-acted-on result in my arc?"* — APPROVE'd-but-unmerged, done-but-unreported, reviewed-but-unforwarded — and act on it rather than waiting for a wake to re-prompt.

Both halves share one primitive: **ground-truth actual state (repo/PR/inbox-mtime/ticket) vs expected arc progress.** Either half alone breaks a given stall; both is defense-in-depth. **Necessary but not sufficient alone:** Layer 2 still only runs inside a turn — it depends on Layer 1 (or a normal wake) to fire that turn.

### Layer 3 (structural backstop — narrow; the "forgot to arm Layer 1" residual) — OPERATOR-GATED

Distinct from rejected P1.c: **not** a continuous monitor of every worker's silence. Instead the narrow signature — a session with an **active team** (an `~/.claude/teams/*/inboxes/` subdir exists, the T-1290 detection) **AND** no activity (commit / Telegram outbound / ledger entry) for > threshold **AND** no pending self-wakeup. The last conjunct is the narrowing: it fires *only* when the dispatcher forgot Layer 1, so it cannot generate the steady-state noise msg_1326 rejected.

This reopens the P1.c-shaped Recovery surface. The incident is the new evidence justifying a re-look; **the operator decides** whether the narrow form earns the cross-account surface, or whether Layers 1–2 alone are the accepted posture (the "forgot to arm" residual then carried by discipline).

### Process lesson (folded into HO discipline regardless of ratification): poke → verify-liveness → decide

On a suspected-dead dispatchee, ground-truth its *actual liveness* (inbox mtime + repo/PR state) immediately after the wake poke and **before** either taking over or issuing contradictory instructions. No speculative parallel takeover. (The incident's recovery whipsawed: wake → belay-I'll-take-it → disregard-belay, creating momentary double-action; harmless only because the duplicate push was idempotent.)

## Generalization + the per-tier mechanism question (OQ-1)

The pattern applies at every dispatcher↔dispatchee boundary. **Layer 1's `ScheduleWakeup` is confirmed at the HO/session tier; whether project-orchestrator SUBAGENTS can arm their own self-wakeup is unverified** (subagents may be request-response within the parent loop rather than independently re-invokable). Two sub-cases, to be resolved by probe:

- **If sub-orchestrators have a self-wakeup primitive:** the pattern is uniform at every tier.
- **If they do not:** sub-tier liveness leans on (a) the parent's Layer-1 watchdog, whose ground-truth already spans the whole arc (HO watchdogs the orchestrator; the orchestrator's own sub-dispatches surface in HO's repo/PR/ticket ground-truth), and/or (b) **restructuring sub-dispatches so completion is a reliable task-notification-to-spawner rather than a member→member SendMessage** — the reliable path per the bug pin. (b) is plausibly the deeper root fix for the sub-tier and is worth evaluating independently.

## The reliable-signal angle (candidate c — ORG-026 mailbox)

The mailbox MCP (ORG-026) is a reliable cross-**persona** channel (orchestrator/specialist/recovery). It does **not** replace Layer 1: (i) it is **poll-based** — `consume` drains, nothing pushes/wakes — so it still needs a poll to fire; (ii) it is **persona-scoped**, so it does not address intra-team subagent (orchestrator↔implementer) signaling. Conclusion: the mailbox improves data-delivery reliability for cross-persona dispatches once a poll fires, but the irreducible primitive remains the dispatcher-side poll (Layer 1). For the subagent case the reliable primitive is the harness **task-notification-to-spawner**, not the mailbox.

## Implementation arc

The T-1315 sub-ticket breakdown (SOP, OQ-1 probe, operator-gated Layer-3 backstop, sub-tier completion-signal evaluation) is in `notes/2026-10-08-org-037-implementation-arc-relocated.md`.

## DECISIONS

- **Layer 1 (self-wakeup) is the load-bearing turn-firer, not a discipline check** — justifies via P1 (only a wake-independent harness primitive fires the turn the discipline checks need) + the load-bearing finding (no arrangement of end-of-turn checks closes a no-turn-fires gap) + incident evidence (the 22:52Z ScheduleWakeup caught the 2nd stall; nothing caught the 1st).
- **Self-wakeup here ≠ rejected ORG-011 Path 3** — justifies via D8/P6 (the HO-drift case had no trigger moment so needed external monitoring; the dispatch-and-wait case has an exact single trigger bound to the dispatch ritual, so self-arming is reliable here) + P1 (`ScheduleWakeup` is a harness primitive, structural, not prose).
- **Bilateral Layer-2 self-checks retained but explicitly marked insufficient-alone** — justifies via the load-bearing finding (they run only inside a turn) + the ticket's defense-in-depth framing (either half breaks a given stall; the pair plus Layer 1 is robust).
- **Layer 3 kept narrow and operator-gated** — justifies via the msg_1326 constraint (no broad worker-silence Recovery monitor) + P1 (the no-pending-wakeup conjunct makes it fire only on the Layer-1-forgotten residual, not in steady state) + the autonomy bar (reopening an operator-closed cross-account surface is his call, not auto-approve).
- **Generalization with OQ-1 surfaced honestly rather than assumed** — justifies via D4/"be the tech guy" (the per-tier self-wakeup availability is a live-system fact to probe, not to assume) + the operator TG 4154 (the org-wide scope is the explicit ask).
- **Mailbox (ORG-026) does not replace Layer 1** — justifies via D5 (literal mechanism: poll-based + persona-scoped) — it is a data-reliability improvement, not a turn-firer.

## Cross-references

- **Incident:** `incidents/2026-06-25-orchestrator-stall-dropped-wake/`.
- **Bug:** `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` (the unreliable primitive; this is its highest-impact manifestation).
- **ADRs:** ORG-011 (P1.a/b landed, P1.c backed out — this reopens a narrower form); ORG-026 (mailbox substrate — candidate c, ruled-out as the turn-firer).
- **Tickets:** T-1315 (this design); T-0225/T-0228 (P1.c land + back-out history); T-1290 (active-team detection via `inboxes/` subdir).
- **Principles/directives:** P1, P6, D1, D8, D10.
