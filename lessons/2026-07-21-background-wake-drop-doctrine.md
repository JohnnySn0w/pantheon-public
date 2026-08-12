# Background-wake doctrine — worker completion wakes are best-effort; output-to-log is the load-bearing mitigation

**Date authored:** 2026-07-21 (T-1915, ORG-012 sweep promotion — 6+ same-shape instances: impl-t1616, impl-t1635, echo-fixarc 2026-07-10, T-1762 ×3 on 2026-07-15)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** the harness changes how subagent background tasks re-invoke (re-test the 3-for-3 evidence before relaxing the doctrine); or the wake-drop rate observed by dispatcher monitors goes to zero across a multi-week window.

## The pattern

A worker's backgrounded long command (build, test suite) finishes, but the harness never re-invokes the worker on completion. The work sits finished-but-uncommitted, the report unsent, until the dispatcher notices and sends a pointed status-check. Source bug: `bugs/teammate-background-task-completion-wake-drop.md`; sibling failure at the inbox hook: `bugs/team-pattern-sendmessage-intermittent-wake-failure.md`.

## Established structural facts (2026-07-15 evidence)

1. **A subagent cannot opt out of backgrounding.** Retrying with `run_in_background` unset and an explicit large `timeout` still auto-backgrounded the long run; workers have no Monitor tool. "Just run it foreground" is not an available mitigation at the worker tier — the exposure is structural for any long-running worker command.
2. **The completion wake is best-effort at the subagent tier.** One session dropped all three of its own completion wakes (clippy ×2, cargo test ×1). This **falsifies "guaranteed"** as previously asserted in `lessons/watchdog-sleeps-must-be-harness-tracked.md` (now amended with the tier caveat). At the *session* tier (HO), `run_in_background:true` tracked tasks + `ScheduleWakeup` remain the right shape — the guarantee failure is only established at the worker tier.
3. **Output-to-log is the load-bearing mitigation.** Every stranded run that had redirected output to a persisted log was fully recoverable — the nudged worker read the log and continued without re-running 14-minute builds.

## The doctrine

**Worker-side (standing brief clause — carried in the org-discipline skill §8):** redirect every long build/test run to a persisted log file before it starts; on any wake gap, read the log and continue from it — never re-run a finished build. Commit promptly after green to shrink the stranded window.

**Dispatcher-side (the guaranteed layer):** treat every worker wake path — background-task completion AND SendMessage inbox delivery — as best-effort; the orchestrator's liveness smell-test is the layer that is guaranteed, because it is pull-based. The detection recipe that beat the role-threshold clocks: a Monitor alerting on (worker transcript stale >20 min) AND (no build processes) AND (no new commit) — caught a drop in ~24 min vs ~80 min under poke-driven detection. Role-threshold clocks (orch 30m / impl 15m / rev 5m) remain the fallback floor.

**Recovery (100% rate so far):** a pointed status-check naming the observed state ("your cargo test finished — log shows green at <path>; commit and report") — not a generic "status?".
