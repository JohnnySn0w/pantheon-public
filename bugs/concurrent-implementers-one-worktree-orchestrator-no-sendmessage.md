# Bug — two implementers writing one worktree concurrently (project-orchestrator has no SendMessage)

**Surfaced:** 2026-06-26, Echo T-1316 Phase-A arc. Filed by Orchestrator (HO); root cause flagged by the `echo-T1316` project-orchestrator from inside the arc.

## Symptom

During a project-orchestrator-led code arc, **two implementer subagents ended up editing the same `/tmp/echo-<task>` worktree concurrently** (~13 min overlap). Observable from outside as ~8 piled-up `cargo check`/`clippy`/`test` invocations contending on the `target/` lock (serialized, each very slow), a worktree that looked "stalled" (transcript mtime frozen during long builds), and a transient "is the tree corrupted?" scare. It converged into one clean commit ONLY because the design was fully locked in the brief (both implementers independently produced the identical `load_vad_silero_fields` helper + the same 3 test names, so the contended diff resolved coherently). **Luck, not safety.**

## Root cause

The `project-orchestrator` agent type has the **`Agent` tool (spawn) but NOT `SendMessage`**. Consequences:
1. It can spawn subagents but **cannot message them mid-flight** — so it cannot cancel a redundant async dispatch once started, nor run an interactive plan-first / reviewer round-trip (only spawn-and-await-completion).
2. Because the orchestrator can't receive/answer a subagent's mid-flight plan message, the implementer's plan-of-record **routed to `main` (HO)** instead. HO, seeing an idle implementer awaiting approval, began managing it directly — while the orchestrator ALSO had its own (redundant) implementer on the same arc/worktree. Two drivers, one worktree.

So the proximate trigger was HO intervening on an implementer the orchestrator had also dispatched; the enabling defect is the orchestrator's inability to coordinate (no SendMessage), which both spawned the routing-to-main and prevented cancellation.

## The tells

- A project-orchestrator arc where the implementer's plan/completion messages arrive at `main` rather than staying inside the orchestrator's team.
- Multiple concurrent `cargo`/build processes on one `target/` (lock contention) with no commit landing.
- A dispatchee that looks "idle/parked" by transcript-mtime but is actually mid-long-build (see mitigation 2).

## Mitigations / fixes

1. **Don't double-drive.** If a project-orchestrator already dispatched an implementer for an arc, HO must NOT spawn or directly manage a parallel implementer onto the same worktree. If HO needs to take over (orchestrator unresponsive), first confirm the orchestrator's implementer is settled/stood-down — ideally halt it — before driving directly. (Here HO began managing `impl-T1316-phaseA` while the orchestrator's async implementer was still live.)
2. **Liveness ground-truth for build-heavy dispatchees = `ps`, not transcript-mtime.** A long `cargo`/`rustc` build freezes the transcript mtime at command-issue time, which reads as "parked" and provokes a premature poke/stand-down (the whipsaw the `dispatch-and-wait-liveness` SOP warns against). Check `ps -eo pid,etimes,comm | grep -E 'cargo|rustc|clippy-driver'` before concluding a stall. → fold into `sop/standard/dispatch-and-wait-liveness.md`.
3. **Structural fix (needs a decision):** either (a) give `project-orchestrator` the `SendMessage` tool so it can coordinate/cancel its subagents and run real plan/review round-trips, or (b) restructure sub-dispatch so completion/coordination rides the reliable harness **task-notification-to-spawner** rather than member→member messaging. This is exactly the sub-tier question ORG-037 OQ-1 + the T-1320 eval already own — feed this incident in as evidence.

## Resolution (2026-06-26 — both deferred mitigations landed)

- **#2 (ps-not-mtime, fold into dispatch-and-wait-liveness)** — DONE. Folded in at [`sop/standard/dispatch-and-wait-liveness.md`](../sop/standard/dispatch-and-wait-liveness.md) §Layer-1 step 3 (the `ps -eo pid,etimes,comm | grep cargo|rustc|…` build-heavy liveness guard, T-1316 lesson cited there).
- **#3 (structural a-vs-b decision)** — DECIDED via T-1320 / ORG-037 toward shape **(b)**: completion rides the reliable **task-notification-to-spawner** path, and the default sub-dispatch is **Agent-subagent spawn with the parent driving each round** (ORG-036). The "give project-orchestrator SendMessage" option (a) was not taken; the parent-drives pattern routes around the no-SendMessage limitation entirely.

## Cross-references

- ORG-037 (team-coordination liveness) §Generalization/OQ-1 + the `dispatch-and-wait-liveness.md` SOP — sub-tier liveness + the reliable-signal angle.
- **T-1320** (Recovery eval: reliable task-notification-to-spawner vs flaky member→member SendMessage) — this incident is direct evidence for option (b).
- `bugs/team-pattern-sendmessage-intermittent-wake-failure.md` — the adjacent wake-reliability bug.
