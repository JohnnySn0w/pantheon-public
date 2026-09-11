---
symptom: "A project-orchestrator spawned as a named teammate (flat-roster) dispatches an implementer/reviewer via the Agent tool, then goes idle awaiting the result. The child's COMPLETION notification (task-notification) surfaces in the MAIN/HO session, not the dispatching orchestrator. The orchestrator never wakes on its own child's completion — it advances only when HO sends it an external SendMessage. Net: a teammate-orchestrator cannot autonomously drive a dispatch→review→dispatch loop in the flat-roster model; it stalls after every Agent dispatch."
fix: "Don't rely on a teammate-orchestrator to receive its own Agent-children's completions. Either (a) HO drives the multi-dispatch arc directly — spawn implementer/reviewer as HO's OWN children so completions land at HO where they reliably surface (the path used to recover T-1333), or (b) keep HO in the loop to relay each child result into the orchestrator via SendMessage (HO→worker wake is reliable). Detect a stalled orchestrator via the end-of-turn worker-staleness smell-test; confirm liveness with ps/file-mtime evidence (the child may be alive and working — don't blind-poke), then take over or relay."
domain: pantheon, agent-tool, team-pattern, flat-roster, project-orchestrator, dispatch-pattern, claude-code-harness
status: "structural-confirmed, standing topology behavior (2026-07-14 re-eval, sweep T-1769) — the 2026-06-26 \"resolved-operationally\" did NOT hold: its remedy (HO-drives-directly) was outrun by ORG-036's self-spawn-by-default posture, under which the identical mechanism recurred 07-10, 07-12, and 07-14 (three arcs, every occurrence same direction). Compensation is now the lead-relay + resume-by-agentId + artifact-poll contract in sop/standard/dispatch-and-wait-liveness.md §Self-spawning intermediate orchestrators. See the one-mechanism cross-links below."
---

> **One mechanism, four entries** (cross-linked 2026-07-14, T-1769): this entry (June, first observation + structural hypothesis) · [`nested-subagent-completion-notification-routes-to-main-not-spawner.md`](nested-subagent-completion-notification-routes-to-main-not-spawner.md) (07-10 recurrence + two-sided compensation) · [`team-subagent-async-worker-completions-notify-session-main.md`](team-subagent-async-worker-completions-notify-session-main.md) (07-12 recurrence + the proven lead-relay working pattern) · [`subagent-cannot-identify-dispatcher-messages-or-reply.md`](subagent-cannot-identify-dispatcher-messages-or-reply.md) (the reverse direction: dispatcher→worker directives are unverifiable/unacknowledgeable in-band). All four are the flat-roster no-true-task-nesting topology, not flakes.

> **Related:** [`team-pattern-sendmessage-intermittent-wake-failure.md`](team-pattern-sendmessage-intermittent-wake-failure.md) (the member→member SendMessage wake-flake — a *different* chain) and [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) (live dispatch guidance). This entry is a distinct mechanism: **parent→child Agent-completion routing**, not member↔member SendMessage delivery.

## Symptom

HO dispatched a `project-orchestrator` (`echo-p4a-orch`) as a named teammate to drive Echo T-1333 (Phase-4a CSM). The orchestrator, lacking the ability to spawn *named* sub-teammates in the flat roster, ran plan-first via two background Agent-tool dispatches (plan-only implementer → review → implementation implementer). Both dispatches' completions routed to HO, never to the orchestrator.

## Evidence (2026-06-27 cycle, T-1333 — 2/2 same direction)

1. Orchestrator dispatches plan-only implementer → emits `idle_notification` (00:06/00:07Z), goes idle.
2. Plan implementer **completes → task-notification surfaces at HO/main** (task `a01409e5`, ~00:?). Orchestrator does **not** wake; never relays the plan summary it promised.
3. HO reviews the plan directly (had it from the task-notification), sends BU rulings via SendMessage → **orchestrator wakes** (HO→worker reliable), folds rulings, dispatches implementation, goes idle (`idle_notification` 00:12Z).
4. Implementation implementer **completes → task-notification surfaces at HO/main** (task `af35e964`, 00:52Z). Orchestrator does **not** wake — idle since 00:10/00:12 with a finished child (43 min) until HO sent a stand-down SendMessage at ~00:53Z.

The orchestrator advanced **only** on HO→worker SendMessage (twice), **never** on its own Agent-children's completions (zero of two). HO detected the stall via live `ps` (cargo test running) + worktree file-mtimes (fresh edits) — liveness evidence, not a blind status-check — then drove reviewer + push + PR + CI + merge directly as HO's own children, where completions reliably surfaced.

## Leading hypothesis (NOT yet isolating-test-confirmed)

The flat-roster team model has no true task nesting: a teammate's Agent-tool dispatch is parented to the **session/main task tree**, so its completion notification routes to the main (HO) loop rather than the intermediate teammate that issued it. If correct, this is **structural**, not the intermittent wake-flake of the sibling bug (which fired reliably 6+ times before failing once). Distinguishing signal: here it failed **2/2, same direction, first try** — more consistent with structural routing than coincidental double-flake.

**Not yet ruled out:** that this is the same wake-hook flakiness manifesting on the completion path, or a desync specific to background (`run_in_background`-style) Agent dispatches. A clean isolating test (spawn a teammate-orchestrator, have it dispatch one Agent child, observe which session the completion notification lands in, repeat N times) would confirm structural-vs-flaky.

## Field evidence (2026-06-30, T-1376/T-1380 echo audit arc — 3/3 same direction)

Further corroboration of the structural (not flaky) hypothesis. HO dispatched a `project-orchestrator` (`echo-audit-orch`) for a 2-ticket echo audit arc; it sub-spawned its own implementer per ticket. **Every** sub-implementer event routed to HO/session-root, never to the dispatching orchestrator:

1. T-1376 implementer **plan-complete → task-notification at HO** (not the orchestrator). Orchestrator separately reported it had *not* received the plan text ("the plan hasn't landed in MY inbox yet").
2. T-1380 implementer **plan-complete → task-notification at HO**. Orchestrator's state was stale (still believed T-1380 "planning").
3. T-1380 implementer **scope-blocker bubble-up (Cargo.toml `test-util` dev-feature) → task-notification at HO**, not the orchestrator.

All three surfaced at HO; HO relayed each down via SendMessage; **orchestrator→child resume/instruct SendMessage worked reliably every time** (no member→member flake on the *downward* leg — consistent with T-1320 directional reliability, parent→child reliable). The arc progressed only because HO acted as the manual child→orchestrator relay. Cumulative across this entry: **5/5 same-direction, first-try, two separate cycles** — strongly structural, not the sibling bug's intermittent-wake-flake. Standing remedy unchanged (HO-drives-implementer+reviewer-directly for completion-driven arcs; this arc was a deviation that confirmed the cost — a per-event manual relay tax on HO attention).

## Operational implication

For multi-dispatch arcs (D7 project-orchestrator pattern), do **not** assume a teammate-orchestrator can autonomously loop dispatch→await→dispatch. Prefer HO-as-direct-dispatcher of implementer+reviewer for arcs where the completions must drive the next step, OR keep HO actively relaying.

## Resolution (2026-06-26 — operationally resolved)

The operational answer shipped: [`sop/standard/dispatch-and-wait-liveness.md`](../sop/standard/dispatch-and-wait-liveness.md) (Adopted 2026-06-26, lands ORG-037) — **the lead/session-root is the locus of re-activation; sub-tier agents cannot self-arm a wakeup (T-1318, structurally confirmed), so the PARENT drives each dispatch round** (ORG-036 default: Agent-subagent spawn, parent-drives; `run_in_background` + parent resumes each round). T-1320 confirmed reliability is **directional** — child→parent task-notification and parent→child SendMessage are both reliable; member→member peer wake is the flaky path.

On the structural-vs-flaky question this bug flagged: the evidence (2/2 same-direction, first try) is consistent with the **no-true-task-nesting** hypothesis — at the **intermediate-teammate tier** (where the spawner ≠ session-root), the Agent-child completion routes to the **session-root** (HO), not the intermediate orchestrator that issued it. So T-1320's "child→parent completion reliable" holds for *parent==session-root*; it does NOT make an intermediate teammate the recipient of its own Agent-children's completions. That tier-sensitivity is now noted in the SOP (§Field-evidence reliable-signal bullet). Net standing pattern: **HO drives implementer+reviewer directly** for completion-driven arcs.
