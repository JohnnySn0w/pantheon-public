---
symptom: "TeamCreate then `Agent` spawn with `name=\"team-lead\"` auto-suffixes the spawned agent to `team-lead-2`; brief that says 'address you as team-lead' is silently false at first-fire because the slot is already occupied by the calling agent (HO)"
fix: "spawn the project-orchestrator with a non-`team-lead` name (e.g. `orchestrator`, `project-orchestrator`, `<arc>-lead`); brief teammates verbatim with the actual name; or pre-read `~/.claude/teams/<name>/config.json` after spawn to confirm member addresses before authoring the dispatch brief"
domain: pantheon, teamcreate, agent-spawn, sendmessage-routing
---


**RETIRED 2026-09-14 — Claude Code 2.1.178 removed `TeamCreate`/`TeamDelete` (T-1281, 2026-06-22).** Teams are implicit now: every session has one, teammates are spawned via `Agent(name=...)` and addressed by that name. The specific pre-allocation collision below cannot happen without an explicit `TeamCreate` call — the mechanism is gone. Kept as-is (per T-1281's "keep-as-historical" disposition) to document past mechanics for anyone reading old cycles on old versions; the current-harness dispatch pattern is what the checklist below carries.

> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) — the live dispatch guidance these symptoms taught now lives in that checklist (ORG-012 supersedure-trail, T-0833 D-1). This entry remains the recorded symptom + diagnosis; act on the checklist for current guidance.

## Symptom

`TeamCreate` then `Agent` spawn with `team_name=<x>` and `name="team-lead"` returns an agent_id suffixed `team-lead-2`. The dispatch brief that told teammates "address the orchestrator as `team-lead`" is silently invalidated — `team-lead` routes to HO (the calling agent's session, which auto-claimed the slot at TeamCreate time), not to the spawned project-orchestrator.

```
$ TeamCreate(team_name="b10-arc")
# Returns: leadAgentId = "team-lead@b10-arc"
# That slot is bound to YOUR session (HO).

$ Agent(team_name="b10-arc", name="team-lead", subagent_type="project-orchestrator")
# Returns: agent_id = "team-lead-2@b10-arc"  ← collision-suffixed
```

Implementer's brief says "report back to `team-lead` via SendMessage." The implementer routes to `team-lead` — which is HO, not the orchestrator. Mid-arc address-correction roundtrips eat cycles + risk silent message-misrouting if not caught.

## Cause

`TeamCreate` pre-allocates the `team-lead@<team_name>` slot bound to the calling agent's session-id. Subsequent `Agent` spawns with `name="team-lead"` collide with that slot and are auto-suffixed.

The b9-validated dispatch pattern (per `incidents/2026-05-04-b9-team-pattern-first-run.md`) said "use system address `team-lead`." That advice was operationally true when the orchestrator WAS the team-lead slot owner; it's silently false when HO retains the slot and spawns a separate orchestrator.

## Fix

**Option A (preferred, first-fire-correct)**: spawn the orchestrator with an explicit non-`team-lead` name:

```python
Agent(
    subagent_type="project-orchestrator",
    team_name="b10-kino-phase1",
    name="orchestrator",  # NOT "team-lead"
    prompt=...,
)
```

Then brief teammates: "the project-orchestrator's address is `orchestrator`."

**Option B**: accept the suffix-collision and brief teammates with the actual address. After `Agent` spawn returns `team-lead-2`, brief verbatim: "the project-orchestrator's address is `team-lead-2`."

**Option C (cleanest for repeatable arcs)**: pre-read the team config after `TeamCreate`+`Agent` spawn and feed actual member names into dispatch briefs programmatically:

```bash
cat ~/.claude/teams/<team_name>/config.json | jq '.members[].name'
```

**Brief verification step**: before sending any dispatch brief, run that `jq` to confirm actual addresses exist + match the brief's prose. Don't write `you are addressed as "team-lead"` as a default — verify.

## Discovered

- 2026-05-05 in b10-kino-phase1 D1 cycle. Same class of failure as `bugs/team-sendmessage-system-name-not-personal-name.md` (system-address-vs-personal-name from b9), one layer deeper — the b9 fix said "use system address `team-lead`" but b10 showed `team-lead` itself can collide.
- 2026-05-08 in b29-echo-binary-wiring — implementer's first plan-READY mis-routed to `team-lead` (= HO) instead of orchestrator (named `orchestrator`); HO forwarded inline; orchestrator corrected for code-tier READY. Confirms Option A pattern: when orchestrator is named non-`team-lead`, the discipline holds first-fire after one inbound mis-route.

## Pairs with

- `bugs/team-sendmessage-system-name-not-personal-name.md` — root cause family (system-address-vs-personal-name routing).
- `bugs/teamcreate-orchestrator-restricted-tool-inventory.md` — TeamCreate-spawned orchestrator's tool restrictions; same spawn-shape as this collision.
- `incidents/2026-05-04-b9-team-pattern-first-run.md` — the b9 retrospective that originated the "use system address" rule.

## Keywords

pantheon, teamcreate, team-lead, slot collision, slot pre-allocation, agent spawn, name collision, suffix auto-suffix, team-lead-2, project-orchestrator, dispatch brief, sendmessage routing, address verification, b9, b10, b29
