---
symptom: "A spawned teammate (Agent-tool worker) runs its authoritative smoke via a background Bash task; the build finishes, but the teammate never wakes on the completion notification — work sits uncommitted and the final report unsent until the orchestrator sends a pointed status-check SendMessage. Distinct mechanism from the inter-member SendMessage wake failure (see team-pattern-sendmessage-intermittent-wake-failure.md): here the dropped wake is the worker's OWN background-task completion, not an inbox delivery."
fix: "Same recovery as the SendMessage variant: orchestrator-side liveness discipline. When a worker's expected long-running artifact (build/test process) is observed finished (ps shows no cargo/rustc; output log shows the terminal marker) AND the worktree shows uncommitted work AND the worker is idle — send a pointed status-check naming the observed state and the exact next actions. Recovery rate so far: 100%."
domain: pantheon, claude-code-harness, agent-tool, background-tasks, dispatch-pattern
---

# Teammate background-task completion wake-drop

## Symptom

Worker pattern: implementer runs the full smoke gate (`tools/run_tests.sh`, ~25-150 min
cold) as a background task in its own session, tells the orchestrator "will report the
moment it's green," and goes idle. The build completes. The worker's harness should
re-invoke it on the task-completion notification — sometimes it doesn't. The worker
stays idle indefinitely; the smoke result exists on disk (log tail says green), the
diff sits uncommitted, and the orchestrator sees only `idle_notification` heartbeats.

## Observed (echo T-1616 / T-1635 arc, 2026-07-08/09, session 58386691)

- `impl-t1616`: full smoke finished green ("All requested tiers green." in its teed log);
  no cargo processes; worktree uncommitted; worker silent. Orchestrator status-check →
  worker's own words: "Background wake did drop silently (as you suspected)." Committed
  + reported within minutes of the nudge.
- `impl-t1635`: same shape same night — build done, everything uncommitted, silent.
  Status-check → full report delivered immediately.
- (Unproven third case the same night: reviewer `rev-t1616` idled 4 min after an
  orchestrator SendMessage carrying data it was waiting on; verdict arrived only after
  a nudge. Could be the SendMessage-variant hitting the HO→worker direction — which
  `~/CLAUDE.md`'s worker-silence section currently claims is reliable — or an ordinary
  turn boundary. Watch for a cleaner instance before amending that claim.)

## Observed (echo build arc, 2026-07-10, session 9e5ee0ab)

- `echo-fixarc` (project-orchestrator teammate): its own harness-tracked
  background commit-poller fired at 02:39Z (exit 0, T-1658 commit detected) but
  the completion never woke it — it discovered the result ~33 min later on a
  manual poll, after an HO status-check prod. Orchestrator inferred the wake
  "landed on main"; HO main did NOT observe such a notification arriving, so
  the verified fact is only wake-never-delivered-to-owner (drop), not misroute.
  Same session as the nested-subagent completion misroute (see relation below)
  — both wake paths failed the same night; poll-based liveness was the layer
  that caught both.

## Orchestrator detection recipe (cheap, three signals)

1. `ps aux | grep -cE "[c]argo|[r]ustc"` → 0 (or the worker's known process gone)
2. worker's worktree: `git status --short` non-empty (work exists, uncommitted)
3. worker idle (last inbox/transcript mtime stale, or an `idle_notification` arrived)

**Refinement (echo-fixarc, 2026-07-10): SendMessage's delivery-mode string is a
free liveness probe.** Sending to a worker returns either "queued for delivery
at its next tool round" — the agent is ACTIVE, merely blocked in a long tool
call (e.g. a cold workspace compile; agents emit nothing mid-bash, so transcript
mtime alone reads falsely stale) — or "was stopped; resumed from transcript" —
the agent HAD gone idle and your message just revived it. Use the probe before
concluding a lane is dead: signals 1-3 all fire false-positive on a worker
sitting in a 20-minute foreground build. Corollary the same night: a worker's
background `cargo test` was SILENTLY KILLED mid-run (pgrep-confirmed, T-1660
lane) — so a vanished process (+ no terminal marker in its log) can also mean
killed-not-finished; the recipe's signal 1 says "investigate", never "conclude
finished" on its own.

All three → send the pointed status-check NOW; name the observed state ("your build
finished, log tail reads X, nothing committed") and the exact next actions. Do not
wait for the role-threshold clock when the artifact-finished signal is already visible.

## Relation to existing entries

- `nested-subagent-completion-notification-routes-to-main-not-spawner.md` —
  sibling: an orchestrator's Agent-subagent completions deliver to the root
  session instead of the spawner. Observed the same night (2026-07-10) as the
  instance above.
- `team-pattern-sendmessage-intermittent-wake-failure.md` — sibling failure at the
  inbox-delivery hook; this one is at the background-task→session re-invoke hook.
  Same posture for both: treat every worker wake path as best-effort; the
  orchestrator's liveness smell-test is the guaranteed layer.
- `feedback_active_team_worker_check.md` (memory pin) + CLAUDE.md worker-silence
  section — the role-threshold clocks there are the fallback; the detection recipe
  above fires earlier when the build-finished signal is directly observable.

## 2026-07-15 evidence — 3-for-3 in one subagent session + foreground is UNFORCEABLE

The T-1762 lane (named-teammate implementer) dropped ALL THREE of its own background-Bash
completion wakes in one session (clippy x2, cargo test x1; each recovered only by an
HO evidence-nudge). Two structural facts established:

1. **A subagent cannot opt out of backgrounding.** The implementer retried with
   `run_in_background` unset AND an explicit large `timeout` param — the harness
   auto-backgrounded the long cargo run anyway. It also has no Monitor tool. So
   "just run it foreground" is NOT an available mitigation at the worker tier;
   the wake-drop exposure is structural for any long-running worker command.
2. **Output-to-log is the load-bearing mitigation.** Both stranded runs were fully
   recoverable because the worker redirected output to a scratchpad log file before
   backgrounding; the nudged worker read the log and continued without re-running
   14-minute builds. Standing worker-brief clause candidate: "redirect every long
   build/test run to a persisted log file; on any wake gap, read the log, don't re-run."

Dispatcher-side detection that worked: a Monitor loop alerting on (transcript stale
>20 min) AND (no cargo/rustc processes) AND (no new commit) — caught drop #3 in
~24 min vs the ~80 min the first one cost under poke-driven detection.

**Pattern lifted to lesson** (ORG-012 criterion 3, T-1915): `lessons/2026-07-21-background-wake-drop-doctrine.md` + standing worker-brief clause in the org-discipline skill §8.

## 2026-08-03 evidence — pattern durable, 3-for-3 re-occurred in one T-1619 subagent

Same 3-drop shape re-observed today on the T-1619 implementer (single named subagent,
one arc): clippy done → drop, `cargo test --workspace` done → drop, `cargo xwin check`
done → drop. Each recovered via HO out-of-team SendMessage naming the observed idle
state; each nudge landed cleanly ("was stopped; resumed from transcript"). No new
mechanism vs 2026-07-15 T-1762; this is confirmation the wake-drop rate on long
Rust build stages is durable, not a T-1762-session-specific artifact. Recovery
cost per drop remained low (~15-30 min silence → nudge → immediate resume + report)
because the implementer's smoke stages logged terminal markers HO could observe from
outside its transcript (per the 2026-07-15 "output-to-log is load-bearing" finding —
the T-1619 implementer had this discipline internalized).

**Elevated frequency worth pinging in a lesson but NOT a policy change:** two full
3-for-3 sessions two weeks apart, both on Rust build stages on echo. Not a rate
increase over 2026-07-15 baseline; same rate confirmed at higher confidence.
HO-side smell-test discipline (transcript mtime + build-activity check) caught
all three drops in the T-1619 arc within their role-threshold windows.
