---
symptom: "SendMessage `to:` an unspawned name returns `success:true`; the message lands in an inbox file with no reader; dispatcher thinks dispatch fired, but no agent ever processes it. Silently fails as 'awaiting-response' indefinitely."
fix: "before declaring 'awaiting-response' on any dispatch, verify the target appears in `~/.claude/teams/<team>/config.json` `.members[]`. SendMessage delivers; it does NOT spawn. Spawn requires the Agent tool."
domain: pantheon, sendmessage, team-dispatch, dispatch-pattern
---


> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) — the live dispatch guidance these symptoms taught now lives in that checklist (ORG-012 supersedure-trail, T-0833 D-1). This entry remains the recorded symptom + diagnosis; act on the checklist for current guidance.

## Symptom

A project-orchestrator (or HO operating inside a team context) fires:

```python
SendMessage(to="implementer", message="<brief>", summary="...")
```

Tool returns `{"success": true, ...}`. Dispatcher logs "brief sent, awaiting plan-tier surface." Hours pass with no plan, no completion, no error. No status update is ever delivered to the dispatcher's conversation turn.

When eventually diagnosed:
- `~/.claude/teams/<team>/inboxes/implementer.json` contains the brief, *unread*.
- `~/.claude/teams/<team>/config.json` `.members[]` does **not** contain any entry with `name = "implementer"`.
- No `Agent` tool call was made to spawn an implementer subagent.

## Cause

`SendMessage` is a **delivery** primitive — it writes to a target inbox file. It is NOT a spawn primitive. The routing layer accepts any string as `to:` and silently creates the inbox file if it doesn't exist. There is no validation that the target name corresponds to a spawned, running process.

Process spawning is a separate primitive: the `Agent` tool. `Agent({name: "implementer", ...})` spawns a subagent process that reads from `inboxes/implementer.json`. Without that spawn, the inbox accumulates messages no one is reading.

The `success:true` return from `SendMessage` indicates **the write to disk succeeded**, not that anyone will ever read it. This is structurally analogous to `mail` returning success on a queued message that has no recipient daemon — the postal pipe is intact, but the mailbox is unattended.

## Fix

**Verification step at dispatch time:**

```bash
cat ~/.claude/teams/<team>/config.json | jq -r '.members[].name'
```

If the intended target is not in the output, the agent does not exist. To dispatch a worker, first call `Agent(name: "implementer", ...)` to spawn it; *then* `SendMessage` to follow up.

**The smell test (load-bearing).** Before declaring any dispatch "awaiting-response":

> Does the target name appear in the team's `config.json` `.members[]`? If not, `success:true` from `SendMessage` means nothing; the message is in an orphan inbox.

Run the check at the moment of dispatch *and* whenever silence-period concerns surface (per `feedback_drift_to_passive_smell_test.md`). The `success:true` ack on SendMessage is not evidence of a live recipient — cross-check before trusting it.

## Cross-references

- **D8 (cross-check claims pre-commit)** applies to dispatch-success acks. The "lean on observed facts before doom-narrative *or* before trust-narrative" rule covers both directions: don't conclude "still broken" without checking async inflight, AND don't conclude "successfully dispatched" without checking the recipient exists.
- **`bugs/teamcreate-orchestrator-restricted-tool-inventory.md`** — ⚠️ that premise is REVERSED (T-1291): the orchestrator HAS `Agent` in team-context (CC 2.1.178+), so it CAN self-spawn — pre-stage no longer forced (self-spawn is the default, ORG-036). The orphan-inbox trap below still applies whenever SendMessage targets a name that was never spawned (e.g. a pre-stage that didn't happen, or a typo'd recipient) — that failure mode is independent of the reversed Agent-restriction premise.
- **`bugs/team-sendmessage-system-name-not-personal-name.md`** — related but distinct. That one is: SendMessage to a real spawned member, addressed by their personal name → lands in wrong bucket of an existing member. This one is: SendMessage to a name that was never spawned at all → lands in an inbox no member reads.

## Discovered

- 2026-05-11T13:08Z during b35 (T-0211 Phase A+C cross-repo team) recovery. team-lead-2 had called `SendMessage(to="implementer", ...)` with the Phase A+C brief at 03:34Z; received `success:true`. Brief sat unread in `inboxes/implementer.json` for ~9.5 hours because no implementer was ever spawned — team-lead-2's persona received `Agent` tool restrictions (per `teamcreate-orchestrator-restricted-tool-inventory.md`), so the orchestrator could not spawn workers itself, and HO had not pre-staged them.
- team-lead-2 surfaced the diagnosis at 13:08Z; HO committed in msg_1277 to file this bugs/ entry.
- Recovery action: team-lead-2 stood down; HO took over orchestration directly. Phase A had already landed via implementer-a (HO-direct, separate path). Phase C was re-dispatched as a fresh HO-direct Agent call after session resume (see ledger entry `2026-05-11T13:55:00Z-001`).

## Keywords

pantheon, sendmessage, success-true-but-nobody-reads, orphan-inbox, dispatch-failure, no-spawn, agent-tool, team-config, members-list, awaiting-response, silent-failure, dispatch-pattern, b9, b35, team-lead-2, project-orchestrator, t0211-recovery, t0217, t0216, smell-test-dispatch
