---
aliases: [ORG-025]
id: ORG-025
title: Specialist orchestration model — interactive-session watch-orchestration with researcher-subagent dispatch (retires headless claude -p watch billing)
status: Ratified 2026-05-29 by the operator (T-0696); trigger mechanism folded in per msg_2683
build_disposition: built
---

# ORG-025 — Specialist orchestration model: interactive-session watch-orchestration with researcher-subagent dispatch

**Status:** **Ratified 2026-05-29 by the operator.** Authored 2026-05-29 by Orchestrator (HO) — Per ORG-015 `adr/` is the ratifiable substrate.

**Ticket:** [[T-0681]] — the operator retargeted the ORG-023 ratify to orchestrator (msg_2639) asking for a critique of ORG-023 against current ops + a delegated decision on whether to elevate Specialist to orchestrator-class ("up to you"). This ADR is that decision.

**Related:** [[ORG-023]] (watch_runner kanban-port decision — this ADR **carries ORG-023's reject-the-custom-kanban verdict** and **supersedes its framing of watch_runner as a permanently sequential headless-`claude -p` python loop**), [[ORG-006]] (topology + the headless-`claude -p` billing cutover ~2026-06-13), [[ADR-212]] (per-MCP context cache — orthogonal, Echo-side), the `researcher` persona (`claude-config/agents/researcher.md`), [[T-0384]] (origin Specialist above-the-bar flag).

## Context

Specialist (`claude-research`) today runs **two** distinct things:

1. A **persistent interactive TUI session** — `tools/systemd/claude-telegram-research.service` launches a long-lived `tmux` `claude` session on the `claude-research` user; it pulls `target=specialist`/`specialist` tickets via its own inbox-consumer (per T-0102).
2. The **research-watch loop** — `pantheon-watch.timer` fires `tools/watch_runner.py` daily (08:00 UTC), which dispatches a **headless `claude -p --agent researcher`** subprocess per due topic, sequentially, tracking only a flat regenerable `state.json` (no claim/lock/heartbeat/retry).

Three findings (investigated 2026-05-29, two background agents) converge:

- **the operator's direction (msg_2638/2639):** elevate Specialist to orchestrate `researcher` subagents (richer than python-fires-headless-`claude -p`), generalizing toward research/writeup orchestration; the orchestrator-class call delegated to HO.
- **Cost cutover:** headless `claude -p` begins drawing paid API / a separate monthly Agent-SDK credit ~**2026-06-13** (ORG-006 amendment 2026-05-21 + the general Agent-SDK-credit change). `watch_runner`'s per-topic headless dispatches become a recurring cost liability at that date (~2 weeks out at authoring).
- **Capability facts (verified against CC docs + the agent-teams doc):** both subagent dispatch (Agent tool) and agent teams require an **interactive session** — neither works in headless `claude -p`. Specialist's existing session **is** interactive (a persistent `tmux claude` via `claude-telegram-research.service`), so both work there; only the watch loop's headless `claude -p` dispatches are the non-team context — and those are exactly what this ADR retires. Agent teams are enabled by the env var **`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`** (settings.json `env` or the service environment) — this is "the flag" set on Specialist's session. Functional difference: **subagents** report to the orchestrator only and never talk to each other (orchestrator manages all work); **agent-team members** each run a full session, **message each other directly**, share a **self-claimed task list**, and can be steered individually — so teams add value only when workers must discuss/challenge/coordinate, not for independent fan-out. Token cost (≈1× per subagent vs ≈7× for teams) is **not** the deciding factor (the operator msg_2654: not a concern at ~20% weekly usage) — functional fit is. The `researcher` persona (`claude-config/agents/researcher.md`, sonnet, leaf worker) works as **either** a subagent **or** an agent-team teammate type, so the choice isn't locked in.

The decisive synthesis: Specialist **already has** the interactive session both subagents and agent teams need, and the `researcher` persona **already exists**. The change is not a rebuild — it is "move watch-orchestration off headless `claude -p` and into Specialist's interactive session," set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` on that session, and dispatch researchers there. That simultaneously delivers the operator's orchestrator-class direction **and** retires the June-13 headless billing cutover.

## Decision

**Elevate Specialist to orchestrator-class** by relocating watch-orchestration from the headless-python-`claude -p` model **into Specialist's existing interactive session**, which dispatches `researcher` subagents via the Agent tool — the HO→worker model, applied to Specialist.

- **Mechanism.** The watch loop (topic cadence, `state.json`, brief construction, failure classification, actuation/ticket-filing, the auto-commit default-safe contract, the HO-cap-hit deferral) is **driven from within Specialist's interactive session**. For each due topic, Specialist dispatches a `researcher` subagent (Agent tool) with the brief, collects the report, lands it under `knowledge/watch/reports/<date>/`, and auto-commits — carrying `watch_runner.py`'s existing discipline forward.
- **Enable agent teams on Specialist's session** (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in the service env) — this opens both dispatch paths. **Default to subagents** for the routine per-topic watch: the topics are independent (one researcher → one report), so there is no inter-researcher coordination for teams to add — subagent fan-out is the functional fit. **Reserve agent-teams** for research that genuinely benefits from cross-talk — multi-angle/adversarial investigation of a *single* topic where researchers challenge each other (the agent-teams doc's top use case). Choice driven by functional fit, not token cost (per the steer, msg_2654).
- **Retires the billing cutover.** Watch dispatch moves off paid-headless-`claude -p` onto the Max-subscription interactive session. **Target completion before 2026-06-13.**
- **`watch_runner.py` is retained, not deleted.** Its reusable logic (topic parsing, cadence/state, failure classification, actuation, default-safe commit contract, cap-hit deferral) is refactored to be driven from / invoked by Specialist's session rather than firing a `claude -p` subprocess. Only the **dispatch mechanism** changes: subprocess-`claude -p` → in-session Agent-tool dispatch.
- **The `researcher` persona stays as-is** (leaf worker). The orchestration discipline (brief quality, kill-criteria, escalation, plan-where-warranted) lives at Specialist's tier, mirroring how HO holds discipline above implementers/reviewers.
- **ORG-023 carried + superseded-in-part.** ORG-023's verdict — reject the custom SQLite kanban — **stands and is reinforced**: the Agent-tool substrate provides the lifecycle/coordination the kanban was being considered for, so no custom kanban is ported. What this ADR supersedes is ORG-023's premise that watch_runner remains a sequential headless-`claude -p` python loop (ORG-023's own #1 re-evaluate trigger: "dispatch moves sequential → parallel / coordination substrate changes").

## Options considered

| | Option | Verdict |
|---|---|---|
| **A** | Keep headless `claude -p --agent researcher` (status quo) | **Rejected.** Hits the ~June-13 billing cutover; cannot orchestrate subagents; against the operator's direction. |
| **B** | Add the team flag to the *headless `claude -p`* watch dispatches | **Rejected — infeasible.** Subagent/team capability is interactive-session-only; no flag enables it in headless `claude -p`. (The flag *does* work on Specialist's interactive session — that's Option C, not this.) |
| **C** | Move watch-orchestration into Specialist's interactive session; set `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; dispatch `researcher` subagents (teams available) via the Agent tool | **Adopted.** Delivers orchestrator-class; retires the billing cutover; reuses the existing researcher persona + watch_runner logic. |
| **D** | Use agent-teams mode (inter-member coordination) for the watch | **Enabled, used selectively — not the default.** The flag is set on Specialist's session, so teams are available. They're the right tool for adversarial/multi-angle research where researchers cross-challenge; they are *not* the default for the routine watch (independent topics → no coordination benefit, subagents fit). Token cost (~7×) is not the gate (msg_2654); functional fit is. |

## Consequences

**positive:**
- Retires the headless-`claude -p` watch billing cutover (moves dispatch onto the Max subscription) — a dated cost win.
- Orchestrator-class Specialist aligned with the HO model; reuses the existing `researcher` persona and `watch_runner` logic; subagent dispatch ≈1× cost (not 7×).
- Reinforces ORG-023: the Agent substrate supplies coordination → no custom kanban, ever.
- Opens richer briefing + multi-step research orchestration (the operator's generalization) without new bespoke infrastructure.

**negative:**
- Requires the watch cadence to **trigger into an interactive session** rather than a clean cron-fires-python model — a new operational shape and failure surface (the interactive session must be alive when a watch is due). *Mitigated by the resolved trigger mechanism below: the cron files a ticket and Specialist's existing inbox-consumer picks it up, so the trigger reuses proven infra rather than introducing new plumbing — but the liveness requirement (session up when the consumer fires) stands, hence the wedge/restart-recovery prerequisite.*
- Migration effort against a ~2-week deadline (before 2026-06-13).
- Couples watch reliability to Specialist's interactive-session liveness (ties to ADR-211 resilience — a wedged/dead session misses watch fires silently).

**deferred:**
- Parallel/concurrent researcher dispatch (Option D; ORG-023's re-evaluate trigger).
- Formal agent-registry persona change elevating Specialist to a named "orchestrator" class (vs operationally orchestrating) — decide if/when it matters.
- Echo's per-MCP context cache (ADR-212) is orthogonal.

**ratification steer (the operator msg_2646, 2026-05-29) — incorporated:**
- Cost: can't afford headless `claude -p` past the ~June-13 API switchover — **confirms the migration off `claude -p` and makes the deadline hard** (not "not worried"; affirmatively unaffordable). Subagent token cost (≈1×) is acceptable.
- Resilience (resolves former open-Q on a watchdog): **yes — give Specialist's session the same wedge/restart-recovery tooling HO runs** (auto-resume, wedge-detection; reuse of the ADR-211 E3/E4 + HO restart machinery). Added as an explicit decision element below.
- Elevation (resolves former open-Q on persona/registry): Specialist is **already** a daemon tmux session like Orchestrator/HO — this is **operational** (finagle the launch flags so the session dispatches subagents + harden it), not a rebuild or a heavy registry change.

**Added decision element (per the steer):** Specialist's interactive session gets the HO-style **wedge/restart-recovery tooling** (auto-resume on cap-hit, wedge/liveness detection, restart machinery) so a scheduled watch is never silently missed when the session is down/wedged. This is a migration prerequisite, not a follow-up.

**trigger mechanism (resolved — the operator msg_2682, 2026-05-29):**
1. **How the scheduled watch cadence triggers into Specialist's interactive session — RESOLVED: the cron injects a ticket into the system for Specialist to pick up.** The `pantheon-watch.timer` (or its successor cron) no longer fires a headless `claude -p`; instead it files a `target=specialist`/`specialist` ticket into the inbox, and Specialist's **existing inbox-consumer** (`pantheon-inbox-consumer@specialist.timer`, per T-0102) picks it up in the interactive session and dispatches the `researcher` subagents. Rationale (the operator): "almost entirely mechanical, and works with existing infra" — reuses the inbox + consumer Specialist already runs, so no new wakeup/timer-poke plumbing is built. This is the inbox-consumer-style inject option, now confirmed over both tmux-send-keys and Specialist self-scheduling.

## Re-evaluate when

- Watch volume grows enough to want parallel dispatch (reopens Option D).
- The Claude Code subagent/headless billing model changes again.
- Specialist's interactive-session reliability proves insufficient for scheduled work (may force a different trigger/supervision model).
