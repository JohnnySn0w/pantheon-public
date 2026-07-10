---
symptom: "TeamCreate-spawned project-orchestrator cannot dispatch its own implementer/reviewer subagents — the spawned orchestrator's tool inventory does NOT include `Agent`/`ToolSearch`/`TeamCreate`/MCP/`Skill`, despite the project-orchestrator persona's standalone tool definition listing `Agent`. The orchestrator wastes turns trying to dispatch implementer-direct, hits the gap, asks for clarification — and a chunk of context is burned."
fix: "HO pre-stages workers as teammates at the same time as the orchestrator. Orchestrator routes the dispatch brief to the implementer via SendMessage (not via Agent dispatch). Reviewer pattern: HO-on-demand one-shot Agent spawn when the orchestrator pings HO at READY review."
domain: pantheon, teamcreate, agent-spawn, dispatch-pattern
---


> **⚠️ PREMISE REVERSED (T-1291, probe-verified 2026-06-24).** The core claim below — that a team-spawned project-orchestrator's inventory does NOT include `Agent` — is **FALSE as of CC 2.1.178+'s implicit-team model**. A probe spawn confirmed the orchestrator HAS `Agent` (effective inventory `Read, Edit, Write, Bash, Agent, SendMessage, Task*`; only `ToolSearch`/`Skill`/`mcp__*` absent). So the symptom/fix frontmatter above describes a since-removed substrate restriction, NOT current reality. **Current guidance (ORG-036 / T-1292): self-spawn is the DEFAULT — the orchestrator spawns its own implementer/reviewer; HO pre-stages only by exception.** This entry is retained as a historical record of the pre-2.1.178 restriction; do NOT act on its frontmatter.
>
> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) + [`adr/ORG-036-dispatch-flow-default-self-spawn.md`](../adr/ORG-036-dispatch-flow-default-self-spawn.md) — the live dispatch guidance now lives there (ORG-012 supersedure-trail, T-0833 D-1).

## Symptom

When HO spawns a project-orchestrator into a team via `TeamCreate` + `Agent` with `team_name=<x>`, the spawned orchestrator's tool inventory is restricted to:

- `Read`, `Edit`, `Write`, `Bash`
- `SendMessage`
- `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`

It does **NOT** receive: `Agent`, `ToolSearch`, `TeamCreate`, MCP tools, `Skill`. This restriction holds despite the project-orchestrator subagent_type's standalone tool definition listing `Agent` (e.g., `Tools: Read, Edit, Write, Bash, Grep, Glob, TodoWrite, Agent` per the agent definition shown in HO's spawn time).

## Cause

`TeamCreate`-spawned agents inherit a restricted tool inventory regardless of their persona's standalone tool spec. The architectural consequence: a TeamCreate-spawned project-orchestrator **cannot spawn its own implementer or reviewer subagents**. The default mental model "orchestrator dispatches implementers/reviewers itself" doesn't hold in this spawn shape.

**Open question closed (T-0217, 2026-05-11).** The `Agent` tool's spawn parameters (per the in-session tool schema) are `description`, `prompt`, `subagent_type`, `team_name`, `name`, `model`, `mode`, `isolation`, `run_in_background`. **There is no tool-grant override.** Tool inventory is determined entirely by `subagent_type` + presence/absence of `team_name`. The restriction is structural to the team-spawn shape, not configurable per-spawn. Confirmed via b35 (T-0211) recovery investigation; persona file at `/home/claude/claude-config/agents/project-orchestrator.md` updated to make the team-context restriction explicit so future fresh-orchestrators don't burn turns rediscovering it.

## Fix

The validated team-pattern dispatch (per `incidents/2026-05-04-b9-team-pattern-first-run.md` + every cycle since b9):

1. HO `TeamCreate`s the team with the project-orchestrator role as lead.
2. HO `Agent`-spawns the orchestrator (e.g., `orchestrator`) with `team_name` set. Brief includes "your team lead is HO; await my workers."
3. **HO pre-stages workers as teammates**: `Agent`-spawns `<team>-implementer` (subagent_type: implementer) and any other long-running workers into the same team via `team_name`.
4. The orchestrator routes the dispatch brief to the implementer via SendMessage (not via Agent dispatch — orchestrator doesn't have Agent).
5. The implementer responds back to the orchestrator (auto-routed via team mailbox).
6. **Reviewer pattern: HO-on-demand, one-shot.** When the implementer's diff is ready, the orchestrator pings HO via SendMessage; HO `Agent`-spawns a one-shot `<team>-reviewer` (subagent_type: reviewer), passes it the diff + propagation-discipline brief, returns the REBOUND/APPROVE verdict to the orchestrator. Lower running-agent count, cleaner reviewer context per cycle.

**Why this isn't a P5 (roles don't hop) violation**: the orchestrator still does orchestration (routing, decision partitioning, justification chain enforcement, escalations); HO's role becomes "scaffolder of the team's worker substrate" + "on-demand reviewer dispatcher." The orchestrator does not role-hop into implementer work — it just lacks the tool to spawn one and asks HO to do it. HO scaffolds, orchestrator routes, implementer codes, reviewer pre-flights.

## Brief-quality discipline

When briefing a project-orchestrator at spawn time, **state explicitly that the orchestrator does not have Agent and that HO will pre-stage workers**. Otherwise the orchestrator wastes turns trying to dispatch implementer-direct, hits the gap, asks for clarification — and a chunk of context is burned. Pre-stage at minimum the implementer at the same time as the orchestrator. Defer the reviewer to on-demand if review cycles are bounded; pre-stage if the team will have many review cycles.

## Discovered

- 2026-05-04 in b9-inbox-consumer team (Pantheon's first real team-pattern run). Original surface diagnosed + fix validated in same cycle.
- Confirmed across every Pantheon Team-pattern cycle since: b10, b15, b16, b17, b19, b20, b23, b24, b25, b26, b27, b28, b29.

## Pairs with

- `bugs/teamcreate-team-lead-slot-pre-allocation-collision.md` — same spawn-shape, different gap (slot collision vs tool restriction).
- `bugs/team-sendmessage-system-name-not-personal-name.md` — orchestrator addresses HO via system-address SendMessage; routing discipline.
- `bugs/sendmessage-body-delivers-summary-preview-only.md` — message body shape for orchestrator↔implementer routing.
- `incidents/2026-05-04-b9-team-pattern-first-run.md` — origin retrospective.

## Keywords

pantheon, teamcreate, agent spawn, project-orchestrator, tool inventory, restricted tools, no agent tool, no toolsearch, pre-stage workers, sendmessage dispatch, reviewer on demand, one-shot reviewer, p5 roles dont hop, b9, dispatch pattern
