---
symptom: "A fan-out worker (observed: Agent-tool general-purpose researcher `rsch-aws`, Specialist session 2026-07-16, T-1787 7-worker telephony fan-out) wedges in a state that defeats BOTH prongs of the dispatch-and-wait-liveness relay contract: (a) emits idle_notification repeatedly (3× over ~2.5 min, 23:57:06/23:58:17/23:59:50Z) without ever sending a report; (b) ignores direct SendMessage nudges ENTIRELY — no reply of any kind to two nudges, including an explicit 'send partial or declare you have nothing' ultimatum (vs the sibling idle-without-report bug where a pointed nudge recovers 11/11); (c) has ZERO task-visibility surface for the artifact-poll fallback — TaskOutput with the agent id errors 'No task found', TaskList returns 'No tasks found'. Dispatcher cannot distinguish 'did research, wedged before sending' from 'did nothing'."
fix: "Presume dead, respawn, never wait. Detection signature = the triple: repeated idle_notification without report + nudge-nonresponse (give one nudge ~2-3 min to land past the idle/message race, then one ultimatum) + TaskOutput/TaskList both blind. On the third leg failing, close the lane honestly as UNVERIFIED (do NOT let a dead lane's absence read as covered — no-silent-caps) and re-dispatch a FRESH agent with the same brief. Do not burn further nudges: nudge-recovery is the signature of the SIBLING bug (subagent-idles-without-sending-final-report.md), and its absence is precisely what distinguishes this one. Candidate third prong for the SOP fallback chain (not yet landed): transcript-file inspection for Agent-tool workers — the spawned agent's transcript JSONL under ~/.claude/projects/<cwd-slug>/ persists even when the task registry is blind; readable evidence of whether work product exists worth salvaging before respawn. Until that lands: timeout-then-respawn is the rule."
domain: pantheon, subagents, agent-tool, fanout, dispatch, liveness, sendmessage, nudge, taskoutput, wedge, respawn
---

# Fan-out worker wedges nudge-nonresponsive with no task visibility — defeats both dispatch-and-wait-liveness prongs

## Occurrence (2026-07-16, Specialist, T-1787 arc — the founding instance)

7-worker research fan-out (6 provider lanes + 1 legal) for the Echo telephony landscape. Six workers exhibited the KNOWN sibling pattern (idle-without-delivering; one pointed status-check SendMessage each recovered all six — consistent with that bug's 11/11 nudge-recovery record). The seventh (`rsch-aws`, Agent-tool general-purpose researcher) was a distinct, worse wrinkle:

- idle_notification ×3 over ~2.5 min (23:57:06 / 23:58:17 / 23:59:50Z) with no report ever sent;
- two direct SendMessage nudges ignored entirely — no reply, including the explicit "send partial or declare you have nothing" ultimatum;
- artifact-poll fallback impossible: `TaskOutput(<agent id>)` → "No task found"; `TaskList` → "No tasks found". Zero visibility surface.

Disposition that worked: the lane was closed as UNVERIFIED in the deliverable (AWS section explicitly marked UNVERIFIED rather than silently absent — `knowledge/episodic/2026-07-16-echo-telephony-call-api-landscape/notes.md`), the shortlist was checked for independence from the dead lane, and the T-1787 resolution note recorded the failure shape. Re-run on request with a fresh agent.

## Why this is a DISTINCT entry vs `subagent-idles-without-sending-final-report.md`

The sibling bug's contract: worker responds to a nudge (relay primary) or leaves a pollable artifact (fallback) — and its record is nudge-recovery 11/11. This instance defeated both prongs AND the nudge signature, so waiting or re-nudging is evidence-free hope. The operational rule the two entries jointly give a dispatcher:

1. idle-without-report → nudge once (after the 2–3 min idle/message-race grace), escalate wording once → sibling bug, expect recovery.
2. nudge-nonresponse ×2 + TaskOutput/TaskList blind → THIS bug → presume dead, close lane as UNVERIFIED, respawn fresh. Never wait.

## Cross-refs

- `bugs/subagent-idles-without-sending-final-report.md` — the recoverable sibling (nudge works there; its lesson promotion is `lessons/2026-07-07-subagent-completion-signal-is-report-plus-idle.md`)
- `sop/standard/dispatch-and-wait-liveness.md` — the relay contract whose two prongs this defeats; third-prong (transcript inspection) + timeout-then-respawn are the candidate hardening from this instance
- T-1769 (relay contract), T-1787 (the arc), T-1791 (operator-directed capture, TG 762/763)
