---
aliases: [ORG-036]
id: ORG-036
title: Dispatch-flow default — project-orchestrator self-spawns workers; HO pre-stages by exception
status: Adopted 2026-06-24 by Orchestrator (HO) under principles-as-ratification (below-goal-level dispatch-methodology decision per the operator msg 3733); D7 directive-text amendment drafted below SURFACED for the operator's async ratification. Resolves T-1292.
date: 2026-06-24
---

# ORG-036 — Dispatch-flow default: orchestrator self-spawns; HO pre-stages by exception

**Status: Adopted 2026-06-24 by Orchestrator (HO).** Below-goal-level dispatch-methodology decision, HO-adoptable under the principles-as-ratification posture (the operator msg 3733: ratify-asks below goal-level retired; HO adopts under P/Ds + ledger, surfaces a lean). The operational propagations (project-orchestrator persona, sop checklist, memory pin) are landed; the **D7 directive-text amendment** drafted in §Propagation is surfaced for the operator's async ratification rather than unilaterally landed (per the T-1292 handoff caution that directive-text edits are his ratification surface). Resolves **T-1292**; unblocks the "default-pending-this-ticket" state in the project-orchestrator persona.

**Related:** [[T-1291]] (probe-verified a team-spawned project-orchestrator HAS the `Agent` tool — the fact this decision unlocks), [[T-1290]] (no-orphan-teammate wording), D7 (decision-flow execution), [[ORG-027]] (role/domain axis).

## Context

T-1291 probe-verified that a team-spawned project-orchestrator HAS the `Agent` tool (implicit-team model, CC 2.1.178+) and CAN spawn its own implementer/reviewer subagents. This **reverses** the pre-2.1.178 restriction that FORCED the HO-pre-stages-workers pattern. Both flows now work:

- **(a) self-spawn** — the orchestrator `Agent`-spawns each worker directly, owning its whole arc.
- **(b) HO-pre-stage** — HO spawns the implementer (and other long-running workers) as teammates when the orchestrator is spawned; the orchestrator routes each unit's brief via `SendMessage`, with the reviewer as an on-demand spawn.

The persona currently says "two flows valid — pick per the brief; default pending T-1292." This ADR decides the default.

## Decision

**Self-spawn is the default. HO pre-stages by exception.**

The project-orchestrator, when a brief is silent on dispatch-flow, **self-spawns** its implementer/reviewer workers via `Agent` and stages its own `agent/<task>` worktrees.

### Why self-spawn as default

- **Locality (P1 — structure matches responsibility).** The orchestrator's purpose is to own a whole arc. Self-spawning keeps worktree-staging and worker-spawning *with the agent that uses them* — no HO↔orchestrator round-trip to pre-stage, no split ownership of the arc.
- **Simplicity.** Fewer HO coordination steps; eliminates the brief-language ambiguity of "which pre-staged teammate does this unit's brief `SendMessage`." The orchestrator spawns exactly what each unit needs.
- **Spawn-depth headroom.** HO(1) → orchestrator(2) → implementer/reviewer(3) sits well within CC-2.1.181's 5-level limit for typical arcs; an implementer spawning a sub-helper (4) still has headroom.

### Why the cons don't override

- **Idle-teammate proliferation** (self-spawn leaves completed teammates idle/un-reclaimable mid-session, per the T-1292 empirical input): harmless — an idle teammate consumes no separate OS process and is session-cleaned. The T-1290 no-orphan rule already targets *mid-task* teammates at session-end, not idle ones.
- **Reduced HO visibility into worker spawns:** mitigated by chain-of-justification (D7) — the orchestrator reports its arc + DECISIONS up to HO regardless of who spawned the workers.

### Pre-stage by exception (HO pre-stages when)

1. A worker needs a worktree/resource only HO can stage (special perms, cross-account ownership, a pre-existing worktree HO controls).
2. A sensitive arc where HO wants direct visibility into / control over worker creation (e.g., live-service touches, production-enable coordination).
3. A genuinely deep nested arc where self-spawn would approach the 5-level spawn-depth limit.

When HO pre-stages, the brief says so explicitly (per D7's "partition what's autonomous vs bubble-up"). **Worktree pre-staging on `agent/<task>` for code workers is separate and still applies regardless of who spawns the worker** — the orchestrator stages it under self-spawn; HO stages it under pre-stage.

## Propagation

- **project-orchestrator persona** (`claude-config/agents/project-orchestrator.md`): resolve "default-pending" → "DEFAULT: self-spawn; pre-stage only when the brief says so or an exception above applies." [LANDED]
- **sop/standard/team-orchestrator-dispatch-checklist.md**: reflect the default. [LANDED]
- **`feedback_pre_stage_workers_before_orchestrator_dispatch` memory pin**: update — pre-stage is the EXCEPTION, not the default. [LANDED]
- **D7 directive** (`rulebook/directives.md`): add one line — "dispatch-flow default is self-spawn (ORG-036); HO pre-stages by exception." [DRAFTED — surfaced for the operator's async ratification; not unilaterally landed per T-1292 handoff caution.]

## Re-evaluate

When CC changes the team/spawn model again (the pre-2.1.178 → 2.1.178 shift is what made this decision necessary), or if spawn-depth limits tighten below 5.
