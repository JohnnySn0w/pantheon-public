---
symptom: "A project-orchestrator subagent trying to spawn a NAMED teammate (Agent tool `name` param) gets the spawn rejected outright — the teammate roster is flat, names are session-root-scoped. Consequences: (1) plan-first must run as two sequential UNNAMED subagent dispatches instead of one persistent named worker; (2) a dead sub-worker cannot be resumed by name after a 529/crash — its context is gone; (3) sub-subagent completion notifications wake-drop to the SESSION ROOT, not to the orchestrator that spawned them, so the orchestrator never hears its own worker finish."
fix: "Design arc dispatches around it: sub-workers commit INCREMENTALLY after every green milestone (commits are the only recovery surface — resume-by-name does not exist one level down); the session root (HO) relays wake-dropped completions to the orchestrator via SendMessage, pointing at the child transcript path under ~/.claude/projects/<cwd-slug>/<session>/subagents/agent-*.jsonl (last long assistant message = the report); orchestrators poll their own spawned-child transcript mtimes rather than waiting on a wake that will not arrive."
domain: "harness, subagents, teams, named-teammate, wake-drop, orchestrator, 529-recovery, pantheon"
---

# Subagents cannot spawn NAMED teammates — flat roster; sub-subagent completions wake-drop to session root

**Found:** 2026-08-12, T-2913 arc (broker-po-3 PO dispatch). The PO's named spawn was rejected outright; its implementer ran as an unnamed subagent whose completion notification went to the HO session root instead of the PO.

## Mechanics

The named-teammate registry (Agent tool `name` param + SendMessage-by-name) is scoped to the session root. A depth-1 subagent (e.g. a project-orchestrator) spawning depth-2 workers gets unnamed children only. Two operational consequences travel together:

1. **No resume-by-name one level down.** The 529-weather playbook "resume the dead worker BY NAME" only works for the session root's own teammates. A PO's dead implementer is unrecoverable as a session; only its transcript and its commits survive. Incremental commits after each green milestone are therefore load-bearing, not hygiene.
2. **Wake-drop to root.** The depth-2 completion notification fires at the session root. The root must relay (SendMessage to the PO with the child transcript path); the PO can also poll child transcript mtimes under the session's `subagents/` dir.

## Verification

Observed twice on 2026-08-12: broker-po-3's named spawn rejected (its process note, T-2913 transition log 20:00:57Z entry); its implementer's plan-completion notification delivered to the HO session root, relayed manually (same log). Re-check by having any subagent attempt an Agent call with a `name` param — rejection reproduces; harness versions after CC 2.1.178 may change this, re-verify before relying on it.
