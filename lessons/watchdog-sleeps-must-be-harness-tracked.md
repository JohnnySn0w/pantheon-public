# Watchdog / heartbeat sleeps must be harness-tracked, not shell-`&` children

**Date authored:** 2026-07-06 (distilled from the ~3.5h HO stall in the T-1526–T-1533 land cycle)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015). **This is a concrete not-used-the-adopted-primitive instance of [`sop/standard/dispatch-and-wait-liveness.md`](../sop/standard/dispatch-and-wait-liveness.md) §Layer 1** — see Cross-references.
**Re-evaluate when:** another stall lands because a self-wakeup was armed in a non-harness-tracked shape (the smell test failed to fire), OR the harness changes how background tasks re-invoke the session.

## Observation

An HO session sat unwoken for ~3.5h mid-wave (2026-07-06 ~04:55–08:25Z) with a merged PR and a worker awaiting plan approval, because its worker-silence heartbeat had been launched as `sleep 1200 … &` INSIDE a foreground Bash call — the detached child was not harness-tracked, so its exit re-invoked nobody. A self-wakeup that the harness can't see is not a self-wakeup.

## Grounding

During the T-1526–T-1533 land cycle, one heartbeat reset was written as a compound foreground command containing `sleep 1200 && echo … &`. The `&` child detached; the foreground call returned instantly; no harness task existed. Every other wake source happened to be quiet for the next 3.5 hours (merge CI completed into a background task whose notification only landed later; the one waiting worker was blocked on approval and sent nothing). Wall-clock loss only this time — but the same gap during a live incident would have been expensive.

This is the SOP's Layer-1 failure mode made concrete: `sop/standard/dispatch-and-wait-liveness.md` §Layer 1 already prescribes arming a self-wakeup via the `ScheduleWakeup` harness primitive precisely so a dispatcher doesn't stall while waiting. The incident used a shell-`&` sleep — a self-wakeup shape the harness cannot track — instead of the adopted primitive. It was a should-have-followed-the-SOP case.

## Design implications

- **The only acceptable shape:** `run_in_background: true` on the Bash tool = tracked task = re-invocation on exit (the Bash-tool analog of the SOP's `ScheduleWakeup`). Never a shell-`&` child of a foreground command: it detaches silently and provides zero wake. **Tier caveat (amended 2026-07-21, T-1915):** the re-invocation is reliable at the *session* tier but is **best-effort, NOT guaranteed, at the subagent tier** — 2026-07-15 evidence: one worker session dropped all three of its own completion wakes, and foreground is unforceable there (the harness auto-backgrounds long runs; workers have no Monitor tool). Tracked-background remains the required shape at every tier — it is still strictly better than `&` — but at the worker tier it must be paired with output-to-log so a dropped wake is recoverable without re-running. Full doctrine: `2026-07-21-background-wake-drop-doctrine.md`.
- **The smell test:** after starting any watchdog, confirm the tool result names a task ID / "Command running in background with ID …". No task ID → no watchdog → fix immediately. If a self-scheduled check can't name *how the wake reaches the session*, it isn't a watchdog.
- **Coverage invariant:** when several async waits are in flight (CI poll, worker approval, heartbeat), at least one must be harness-tracked at all times; the heartbeat exists precisely for the window where everything else goes quiet.

## Open questions

- Should the smell test be structurally enforced (a hook that flags foreground commands containing `sleep … &`) rather than a discipline? P1 would prefer that; the recurrence count (1) doesn't yet justify the build.

## Cross-references

- **Owning SOP:** [`sop/standard/dispatch-and-wait-liveness.md`](../sop/standard/dispatch-and-wait-liveness.md) §Layer 1 — arms a self-wakeup (`ScheduleWakeup`) when you dispatch-and-wait. This lesson is the concrete failure instance of that layer not being followed; the `run_in_background:true` rule here is the same Layer-1 principle at the Bash-tool tier.
- Backing ADR: [`adr/ORG-037-team-coordination-liveness.md`](../adr/ORG-037-team-coordination-liveness.md) (the liveness layers).
- Memory pin: `feedback_watchdogs_must_be_harness_tracked`.
- Directives: P1 (structural over vigilance), P7 (cheap self-wakeup over a stalled wait).
