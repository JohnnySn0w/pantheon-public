---
symptom: "When a named teammate (e.g. a project-orchestrator spawned via the Agent tool) spawns its own workers via the Agent tool, those spawns run async even when foreground is intended, and the worker's completion task-notification (plan turns and final completion reports alike) is delivered to SESSION MAIN (the team lead / HO) rather than to the spawning teammate, which sits idle believing it will be woken."
fix: "Lead relays each task-notification's essentials plus the task output-file path to the spawning project-orchestrator via SendMessage; the project-orchestrator reaches its own worker directly by raw agentId (not by re-dispatching a fresh worker — the stopped worker's context survives and resumes intact) for approvals/instructions; brief both sides up front that worker completions land on the lead and budget the extra relay round-trips into the arc plan."
domain: pantheon, claude-code-harness, agent-tool, nested-subagents, team-pattern, dispatch-pattern, sendmessage
---

# Team-pattern: a subagent's async worker completions notify SESSION MAIN, not the spawning subagent

**Harness:** Claude Code implicit-team model (post-2.1.178, TeamCreate/TeamDelete removed). Sibling of `team-pattern-sendmessage-intermittent-wake-failure.md`.

## The behavior

When a named teammate (e.g. a project-orchestrator spawned via the Agent tool's `name` param) spawns its OWN workers via the Agent tool:

1. **Named-teammate spawn is blocked inside the flat team** — the PO cannot create further named teammates; its workers become plain subagents.
2. Those subagent spawns run **async even when the spawner intends foreground** — the PO's turn ends, the worker keeps running.
3. The worker's completion **task-notification is delivered to SESSION MAIN (the team lead / HO), not to the spawning subagent** — every stop of the worker (plan turn, completion report) lands on the lead's side. The spawning PO never sees it unless someone relays.

Evidence: T-1633 arc, 2026-07-12 — three consecutive occurrences (Unit-1 implementer plan turn, Unit-1 implementer completion, Unit-1 reviewer completion) all notified HO while the PO idle-waited. 100% consistent, so treat it as the topology's behavior, not a flake.

Further: T-1757 arc, 2026-07-14 — implementer pre-flight guard-halt AND its plan-first stop both notified HO, not the spawning PO (po-t1757); the PO's resume-by-agentId SendMessage worked first try, context intact. Cumulative across the four cross-linked entries: 11+ occurrences, four arcs, zero exceptions.

> **One mechanism, four entries** (cross-linked 2026-07-14, T-1769): `teammate-orchestrator-agent-dispatch-completion-routes-to-main.md` (June original, status now structural-standing) · `nested-subagent-completion-notification-routes-to-main-not-spawner.md` (07-10) · `subagent-cannot-identify-dispatcher-messages-or-reply.md` (reverse direction). SOP contract: `sop/standard/dispatch-and-wait-liveness.md` §Self-spawning intermediate orchestrators.

## Working pattern (proven same arc)

- **Lead relays**: on each task-notification, HO SendMessages the essentials + the task output-file path (`/tmp/claude-<uid>/<cwd-slug>/<session>/tasks/<task-id>.output` — readable by the teammate) to the spawning PO.
- **PO reaches its own worker directly** for approvals/instructions — SendMessage to the worker's raw agentId works from the PO's side (used for the plan-approval turn; the worker resumes with context intact).
- Brief both sides up front: tell the PO "worker completions may land on the lead; I'll relay" and tell the lead-side to expect the dance. Budget the extra round-trips into the arc plan.
- Do NOT re-dispatch a fresh worker just because its report went to the other tier — the stopped worker's context (plan, file reads) survives; resume it by agentId.

## Don't

- Don't diagnose the PO as "stuck/silent" when it's idle-waiting on a worker whose completion you (lead) are holding — check your own task-notifications first.
- Don't have the PO poll worker transcripts as a workaround — the notification WILL arrive, just at the lead.

## Keywords

task-notification, session main, spawning subagent, project-orchestrator, Agent tool,
flat team, named-teammate spawn blocked, async completion, idle-waiting, lead relay,
agentId resume, SendMessage, plan turn, completion report, T-1633, T-1757,
dispatch-and-wait-liveness, self-spawning intermediate orchestrators, po-t1757
