---
symptom: "A project-orchestrator (itself an Agent-tool teammate) spawns implementer subagents async; when an implementer finishes, the task-notification (with the full final report) is delivered to the TOP-LEVEL session (HO main), not to the spawning orchestrator. The orchestrator sits idle believing it will be woken on completion; the report it needs is in a session it can't see."
fix: "Two-sided compensation. HO-side: when a completion lands that belongs to an orchestrator's lane, forward the substance (or the ruling, if HO-gated anyway) via SendMessage to the orchestrator immediately. Orchestrator-side: brief it to POLL its subagents' state (worktree commits, process liveness) when idle rather than trusting completion-wakes. Detection recipe in teammate-background-task-completion-wake-drop.md applies."
domain: pantheon, claude-code-harness, agent-tool, nested-subagents, dispatch-pattern
---

# Nested-subagent completion notification routes to main, not the spawner

## Symptom

Dispatch chain: HO (main session) → project-orchestrator (named teammate) →
implementers (the orchestrator's own async subagents, flat roster). When an
implementer completes, the `<task-notification>` carrying its final report is
injected into the HO main session, not the orchestrator's. The orchestrator —
which told its workers "report on completion" and believes it will be woken —
never sees the completion unless it polls or HO forwards.

## Observed

- 2026-07-09/10 session (pre-restart, per HANDOFF.md): "echo-fixarc … was polling
  its implementers directly (completion-wakes route to main this session)" — the
  predecessor orchestrator had already adapted by polling.
- 2026-07-10 ~02:38Z (session 9e5ee0ab, post-restart): echo-fixarc's T-1658
  investigator (agent "Investigate T-1658 TTS re-synth", task a7a42a9ae99e08806)
  completed; the full plan-first report arrived as a task-notification in the HO
  main session. HO ruled on the plan directly and forwarded the ruling +
  routing heads-up to echo-fixarc. Two sessions, same shape — recurring, not a
  one-off.

## Unknown: design or bug

May be intentional harness behavior for nested spawns in this CC version
(completion notifications bubbling to the root session) rather than a defect.
Operational posture is identical either way; if a CC changelog later names this,
update this note. Do NOT assume the orchestrator's "my subagents report to me on
completion" belief is true in ANY session until a completion is observed landing
orchestrator-side.

## Compensation (both sides, standing)

1. **HO-side forward:** a completion landing in main that belongs to an
   orchestrator's lane gets forwarded promptly — either the substance, or (when
   the item was HO-gated anyway, e.g. a plan-first ruling) the ruling itself,
   plus an explicit routing heads-up so the orchestrator recalibrates.
2. **Orchestrator-side polling:** dispatch briefs for orchestrators that spawn
   their own workers should say completion-wakes may not reach you — poll worker
   state (worktree `git log`/`git status`, process liveness) when idle. The
   predecessor echo-fixarc's polling pattern is the proven workaround.

## Relation to existing entries

- `teammate-background-task-completion-wake-drop.md` — sibling: a worker's OWN
  background-task wake drops. Same family posture: every wake path is
  best-effort; liveness discipline is the guaranteed layer.
- `team-pattern-sendmessage-intermittent-wake-failure.md` — sibling at the
  inbox-delivery hook.
- `~/CLAUDE.md` §Active-team worker silence — the role-threshold smell test is
  the catch-all fallback for all three mechanisms.
