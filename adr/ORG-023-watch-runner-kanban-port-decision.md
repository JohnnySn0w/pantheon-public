---
aliases: [ORG-023]
id: ORG-023
title: watch_runner kanban-primitive port decision — reject full port, conditional minimal circuit-breaker
status: Superseded 2026-05-29 by ORG-025 (verdict carried; framing superseded)
---

# ORG-023 — `watch_runner` kanban-primitive port decision (reject full port; conditional minimal circuit-breaker)

**Status:** Superseded 2026-05-29 by [[ORG-025]]. The reject-the-custom-kanban verdict is **carried forward** into ORG-025 (the Agent-tool substrate provides the coordination the kanban was for). What ORG-023 framed — watch_runner as a permanently sequential headless-`claude -p` python loop — is **superseded**: ORG-025 moves watch-orchestration into Specialist's interactive session. Originally Authored 2026-05-29 by Orchestrator (HO); the operator retargeted its ratify ticket (T-0681) to HO for critique (msg_2639) rather than ratifying, and the critique+decision became ORG-025.

**Ticket:** [[T-0384]] (above-the-bar finding lifted by Specialist from the 2026-05-17 3rd-party-harnesses survey delta §4.1, msg_45). Research input: `knowledge/episodic/2026-05-29-hermes-agent-kanban-primitive/notes.md` (sourced from NousResearch/hermes-agent v0.13.0 official docs + release notes + open issue #25517).

**Related:** [[ORG-012]] (information-inflow bubble-up — this is the ADR step of an above-the-bar Specialist flag), [[T-0357]] (worktree worker-isolation — intersects any reclaim design), [[ORG-017]] (knowledge tree — research deliverable path).

## Context

Specialist flagged, above-the-bar, that the hermes-agent (NousResearch) **Multi-Agent Kanban** primitive — heartbeats, task reclaim, zombie-detection, per-task retry budgets, backed by a SQLite state store — is "genuinely worth lifting" into Pantheon's `tools/watch_runner.py` dispatch layer, on the grounds that Pantheon's current multi-instance coordination is implicit and human-mediated.

**The primitive, concretely (v0.13.0):** SQLite-backed (`~/.hermes/kanban.db`, WAL); a `tasks` table with a 7-state lifecycle (triage→todo→ready→running→blocked|done|archived), a `claim_lock` (`host:pid:uuid`), a `consecutive_failures` circuit-breaker counter, and `max_retries`; an append-only `task_events` audit log + `task_runs` attempt log. A dispatcher ticks every 60s: Phase-1 reclaims tasks whose 15-min claim TTL expired (compare-and-swap to `ready`), Phase-2 detects crashed workers via `kill(pid,0)` and reaps zombies. After `consecutive_failures` hits the limit (default 2) the dispatcher emits `gave_up` and auto-blocks pending human unblock. Known open defect #25517: a single blocking tool call longer than the 15-min claim TTL produces false-positive stale_lock reclaims + duplicate workers (the worker can't heartbeat while a tool call is pending).

**Pantheon's current pattern:** a flat `state.json` (`~/.local/state/pantheon/watch/state.json`, schema_version 1) recording only `last_fire` + `last_outcome: ok|fail` per topic. Topics fire **sequentially** (one `claude -p` subprocess each), single-threaded, no claim/lock, no heartbeat, no retry counter. Timeout = a 30-min wall-clock `proc.kill()`; failures are classified (`task_error | rate_limit_halt | rate_limit_halt_ho_correlated`) and logged but do **not** drive re-dispatch — the only retry is the next cron fire if the cadence is due. ~4 active topics, daily/weekly cadences. state.json is intentionally **regenerable from the reports/ filesystem dates** if lost.

## Decision

**Reject the full kanban port.** Adopt, at most, a **minimal selective borrow** — a `consecutive_failures` counter + `gave_up` circuit-breaker added to the existing `state.json` — and only **conditionally**, gated on a failure-distribution check (below). Do **not** adopt the SQLite store, the tick-based dispatcher, claim-locks, the heartbeat daemon, PID-probe zombie detection, or parallel claim-and-spawn.

**Conditional gate on the minimal borrow:** before implementing even the circuit-breaker, inspect the `watch_runner` log for the actual failure-mode distribution. If recurring failures are dominated by `rate_limit_*` (the expected case), the circuit-breaker adds little — a rate-limited topic that "gave up" would just be a topic Pantheon wants to retry next cadence anyway, and immediate same-run retry burns tokens re-failing. In that case, **defer even the minimal borrow** and close the adoption question as "cadence-retry sufficient; revisit if non-rate-limit recurring failures emerge." If the log shows a class of topics failing every fire for non-rate-limit reasons (malformed brief, persistent tool error), implement the minimal circuit-breaker so those stop re-firing and surface as `gave_up`.

## Options considered

| | Option | Verdict |
|---|---|---|
| **A** | **Full port** — SQLite store, tick dispatcher, claim/reclaim, heartbeat, zombie-probe, retry budgets | **Rejected.** Solves a multi-machine / concurrent-worker / parallel-dispatch topology Pantheon does not have. Sequential single-VPS dispatch makes claim/reclaim/parallel machinery inert; `proc.kill()` at 30 min already handles the crash/zombie case the 60s tick would. Trades the regenerable-state property of state.json for a new recovery surface (corrupt DB, WAL, lock contention). Open defect #25517 directly bites Pantheon: researchers routinely run multi-step WebSearch/WebFetch sequences inside one `claude -p`, exceeding a 15-min claim TTL → false-positive reclaims + duplicate workers. ~200–400 LoC rewrite + SQLite migration for disproportionate value at ~4 topics. |
| **B** | **Minimal selective borrow** — `consecutive_failures` + `gave_up` on existing state.json; no dispatcher rewrite | **Conditionally adopted** (see gate). The one feature with genuine standalone value: distinguishes "fails every run, broken" from "failed once, transient," and stops re-firing persistently-broken topics. Cheap (additive state field + a counter check), preserves state.json's regenerable property. |
| **C** | **Shorter-cron retry** — for topics that fail, schedule a shorter cron interval rather than same-run retry | Noted as a simpler alternative to same-run retry if the gap-closing motivation (a failed weekly topic losing a 7-day window) proves real. Does not require any dispatcher change. Available as a follow-up if the circuit-breaker proves insufficient. |
| **D** | **Heartbeat-only (observability)** — proxy stream-json events as a liveness signal | Deferred. Weaker than hermes-agent's model (stream events stop during inference, not just tool calls); only worth it if in-flight observability becomes a felt need. |

## Consequences

**positive:**
- Pantheon avoids adopting a coordination primitive built for a topology it doesn't have; complexity stays proportional to the ~4-topic, sequential, single-VPS workload.
- state.json keeps its regenerable-from-filesystem property (no new SQLite recovery surface).
- If the conditional gate opens, the circuit-breaker is a small additive change that makes persistently-broken topics visible (`gave_up`) instead of silently re-failing each fire.

**negative:**
- No same-run recovery: a topic that times out or crashes still waits for the next cron fire (24h for daily, ~7d for weekly). Accepted — the research found no evidence this gap has hurt, and the dominant failure mode (rate-limit) wouldn't benefit from same-run retry anyway.
- The decision rests on a topology assumption (sequential dispatch, few topics) that, if it changes, reopens the question — see re-evaluate trigger.

**deferred:**
- Same-run retry / parallel dispatch — revisit only if watch dispatch becomes parallel (Option A's main structural win is parallelism Pantheon intentionally forgoes today).
- Heartbeat observability (Option D) — revisit if in-flight researcher progress visibility becomes a felt need.
- The T-0357 worktree-invariant integration (if a reclaim ever spawns a replacement researcher, who pre-stages its worktree?) — moot under this decision since no reclaim machinery is adopted; would need resolving before any future Option-A reconsideration.

## Re-evaluate when

- Watch dispatch moves from sequential to parallel multi-worker (reopens Option A — parallel coordination is the kanban's real win).
- The failure-mode distribution shifts: a class of non-rate-limit recurring failures emerges (opens Option B's gate) — or rate-limit ceases to dominate.
- Active watch-topic count grows by an order of magnitude (changes the complexity-vs-workload calculus).
