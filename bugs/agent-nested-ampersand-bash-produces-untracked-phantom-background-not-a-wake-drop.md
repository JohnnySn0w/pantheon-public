---
symptom: "Agent (Agent-tool worker) launches its own long-running smoke via `bash -c 'cmd &'` or a nested wait-loop pattern inside a synchronous Bash tool call, then goes idle expecting a task-completion notification. The notification never arrives because the harness never TRACKED that background — the harness only notifies on tasks it launched itself (run_in_background:true on the Bash tool, or its own auto-backgrounding when a foreground call exceeds ~600s cap). A nested-& child is an untracked orphan from the harness's POV, and task-notifications keep firing saying 'no live background children of its own' — accurate: there is no CHILD the harness tracks, only a grand-child at OS level."
fix: "Never spawn agent long-running work via nested bash `&` + wait-loop. Two supported paths for a >2min job: (1) pass `run_in_background: true` on the Bash tool call — the harness tracks it and notifies on completion; (2) run synchronously with `timeout: <ms>` (Bash tool max 600000 = 10min) OR let the harness's own auto-background kick in (foreground calls that exceed the 600s foreground cap are automatically re-parented into the tracked-background lane). Recovery when stuck: orchestrator sends a pointed status-check naming the observed state; worker re-launches via the supported path."
domain: pantheon, claude-code-harness, agent-tool, background-tasks, dispatch-pattern
---

# Agent nested-`&` Bash produces untracked phantom background — distinct from a wake-drop

## Symptom

Agent-tool worker (impl-t3323 arc, 2026-08-28) hits a smoke-loop timeout ceiling on a 22-minute `pytest tools -q` and works around it with a pattern like:

```bash
bash -c 'pytest tools -q > /tmp/out 2>&1 &'
# … then in a follow-up Bash tool call:
while [ ! -s /tmp/out ] && kill -0 $BGPID 2>/dev/null; do sleep 30; done
tail -40 /tmp/out
```

Impl then stops and waits for a task-completion notification. Task-notifications keep firing — but each one carries the note "no live background children of its own." Impl reads that as confirmation nothing is running, but the pytest process IS running (visible via `ps -u <impl-uid>` from outside; PID lives, CPU accumulating). Impl loops on repeated stop → notification-fires-with-no-children → nothing-changes for the orchestrator to observe.

Distinct from `teammate-background-task-completion-wake-drop.md`: THAT bug is a proper harness-tracked task whose completion notification was DROPPED (the harness knew, the wake failed). THIS bug is a task the harness never knew about at all — nothing to notify on.

## Verification / Observed (impl-t3323, T-3323 arc, 2026-08-28)

Impl parked in stop-wait for ~3 cycles before HO's pointed status-check ("STOP waiting on bt8f18o0z — the harness reports 'no live background children' on each notification; you are looping on a phantom wait"). Impl then relaunched pytest through the harness's supported path — a synchronous Bash tool call that exceeded the 600s foreground cap and was auto-backgrounded into the tracked-task lane, which THEN produced a proper completion notification when the 22min suite finished.

Impl's own post-arc reflection: "the harness's own auto-backgrounding mechanism (triggered when a foreground call exceeds its cap) produced a properly tracked, notifying task, while my nested-`&`-plus-wait-loop pattern produced an untracked 'phantom' background process the harness couldn't notify on."

## Cause

The harness tracks background tasks it launches — via `run_in_background: true` on the Bash tool, OR via its own auto-backgrounding when a foreground Bash call exceeds the ~600s foreground cap. A nested `&` inside a Bash tool call OS-forks a child that becomes a grandchild of the harness (or an orphan reparented to systemd --user). The harness has no handle to it. Task-completion notifications only fire for tasks the harness's own task-tracker registered. An untracked bg has no such registration; its completion is silent from the harness's POV.

## Fix

Never spawn agent long-running work via nested bash `&` + wait-loop. Two supported paths:

1. **Explicit tracked-background**: pass `run_in_background: true` on the Bash tool call. The harness assigns a tracked task ID, and its completion notification arrives via the same task-notification pipe the plan-first/dispatch pattern already relies on.
2. **Synchronous with generous timeout**: pass `timeout: <ms>` up to 600000 (10min) for foreground. For jobs that will exceed 600s, DO NOT try to work around the cap with nested `&` — the harness will AUTO-background the exceeded call itself, and THAT auto-background IS tracked. So just launch synchronously with `timeout: 600000` and let the harness re-parent.

Do NOT use nested `bash -c '… &'` inside a Bash tool call for anything longer than a few seconds — the exit code will report the shell's exit, not the backgrounded job's; the harness won't track completion; and any `wait` loop inside a follow-up Bash call is polling a shell-detached PID that the harness cannot observe.

## Recovery (when a worker is already stuck)

Orchestrator sends a pointed status-check via SendMessage naming:
- What the harness sees (task-notification "no live background children")
- What actually exists on disk (`ps -u <uid> | grep <expected process>`, or a tail of the expected output log)
- The exact next actions: kill the orphan if present, relaunch via `run_in_background: true` or synchronous+long-timeout

Worker recovers on the next re-invoke. Recovery rate so far: 1/1 (T-3323 case).

## Related

- `teammate-background-task-completion-wake-drop.md` — distinct: harness-tracked task, wake-drop after completion. THIS one is untracked-from-the-start.
- `background-workload-bare-ampersand-orphans-on-sigkill-run-it-in-a-scope.md` — distinct: bare-& runaway load, not agent smoke-suite pattern.
- `feedback_watchdogs_must_be_harness_tracked.md` (HO memory pin) — same principle at the HO tier: watchdog sleeps must be harness-tracked via run_in_background:true.
