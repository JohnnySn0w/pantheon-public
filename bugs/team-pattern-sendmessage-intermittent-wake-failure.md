---
symptom: "SendMessage between two spawned team-pattern members (both in `.members[]`) returns `success:true`; recipient never wakes; the message never surfaces in their conversation context as a `<teammate-message>` inject. Both ends sit idle indefinitely (observed: 5h27m until HO manual intervention)."
fix: "treat the auto-wake hook for team-pattern inbox delivery as best-effort, not guaranteed. Catch via HO end-of-turn smell-test on `~/.claude/teams/` worker-staleness (codified in CLAUDE.md + feedback_active_team_worker_check.md); recover via HO-direct status-check SendMessage to the silent worker. The harness-internal delivery hook is closed-source and not directly fixable from Pantheon."
domain: pantheon, sendmessage, team-pattern, claude-code-harness, dispatch-pattern
---


> **Superseded as load-bearing by** [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) — the live dispatch guidance these symptoms taught now lives in that checklist (ORG-012 supersedure-trail, T-0833 D-1). This entry remains the recorded symptom + diagnosis; act on the checklist for current guidance.

## Symptom

A team-pattern worker (e.g. implementer) fires:

```python
SendMessage(to="team-lead", text="READY — Unit N complete. Branch ...")
```

Tool returns `{"success": true, "message": "Message sent to team-lead's inbox", "routing": {"sender": "...", "target": "@team-lead", ...}}`.

Both sender and recipient are in `~/.claude/teams/<team>/config.json` `.members[]` (verified pre-dispatch — this is **NOT** T-0216's orphan-inbox pattern).

The recipient — sitting idle from a prior turn that ended with text like *"Idle pending implementer's READY"* — never wakes. The inbox-message is **not** delivered as a `<teammate-message teammate_id="...">` user-inject on the recipient's next turn. The recipient produces no further snapshot JSONL until externally woken (e.g. HO status-check SendMessage).

Observed concretely in b36 cycle, 2026-05-11:
- Implementer SendMessage to team-lead at `2026-05-11T18:08:10Z` → `success:true`.
- Orchestrator (team-lead) last snapshot before silence: `agent-a5110a1d` mtime `18:00:24` ending with text *"Idle pending implementer's code-tier READY on the final Unit 3."*
- Next orchestrator snapshot: `agent-a280a572` mtime `23:35:08` — 5h27m later — woken by HO status-check at `23:33:36`.
- Orchestrator's first response when woken: *"I have NOT been working silently — I have no record of receiving Unit 3 READY from implementer."*
- Same orchestrator had successfully woken on implementer SendMessages at Unit 1 (~54s latency) and Unit 2 (~66s latency) earlier in the same cycle. The wake-hook fired reliably 6+ times before failing once.

## Cause

The team-pattern's `SendMessage → recipient-wake → conversation-context inject` chain has a step that occasionally fails to fire. The chain is:

1. **Sender's `SendMessage` tool call**: writes payload to recipient's inbox file. Returns `success:true` on disk-write success. ← *this step is reliable*.
2. **Wake-hook**: triggers a new turn for the (idle) recipient process, injecting the inbox content as a `<teammate-message>` user-content block. ← *this step is intermittently flaky*.
3. **Recipient's turn**: produces a new JSONL snapshot under `~/.claude/projects/<slug>/<session>/subagents/`. ← reliable once step 2 fires.

The hook is internal to Claude Code's team-pattern subsystem and not directly inspectable from Pantheon. The failure mode resembles a worker-process-state desync (hypothesis 4 in T-0226): the idle worker may cache a state that prevents the inbox-poll from firing on the next delivery, even though it fired correctly on prior deliveries within the same cycle.

The `success:true` return tells you **the disk-write succeeded**, NOT that the recipient will read it. This is structurally similar to T-0216 (`sendmessage-without-spawn-dispatch-failure.md`) but the failure mechanism is different:

- T-0216: target not spawned → orphan inbox, deterministic, fixed by pre-spawn check.
- **This bug**: target spawned + in members + woke reliably moments earlier → wake-hook silently no-ops, intermittent, no Pantheon-side fix available.

## Fix

**No fix at the message-delivery layer is available from Pantheon** — the wake-hook is in Claude Code's harness. Treat team-pattern SendMessage as **best-effort delivery**, not guaranteed.

**Primary detection: HO end-of-turn smell-test** (codified in `/home/claude/CLAUDE.md` + `feedback_active_team_worker_check.md`). If `~/.claude/teams/` is non-empty at end of turn, HO IS attending that team (no legitimate state where HO is running with an active team and not driving it). For each non-lead member, check inbox/transcript mtime against role-thresholds (orchestrator 30 min / implementer 15 min / reviewer 5 min). If any worker exceeds AND HO didn't actively SendMessage them this turn → status-check immediately. The check is a cheap `ls ~/.claude/teams/*/inboxes/` followed by mtime comparison against current time.

**Recovery (operational protocol):** send a direct status-check SendMessage to the silent worker from outside the team. The teammate-inject from HO reliably wakes the worker (HO→worker SendMessage from outside the team has not exhibited the same flakiness in observed history; the wake-hook bug is between team-pattern members). The worker can then re-engage from the freshly-injected status check, even if their inbox had been silently undelivered.

**Sender-side integrity check (optional, not implemented):** the sender could schedule a follow-up to itself ("if no reply from team-lead within N min, ping HO directly"). This would catch the bug from the sender side. Not adopted because (a) it adds protocol surface at every SendMessage, and (b) HO's end-of-turn smell-test catches it cleanly without per-message overhead.

**Historical note: T-0225 P1.c was overengineered for a state that doesn't exist.** An earlier iteration of this entry pointed at T-0225's Recovery-side activity-monitor P1.c worker-silence predicate as the primary detection. Per the operator verdict msg_1326 (2026-05-12), that framing was reset: HO is by definition attending any active team; "HO-absent + team-active steady state" isn't a real case. P1.a + P1.b cover the legitimate HO-absent + queue-existing case (cap-hit, between-sessions). P1.c is being backed out (T-0228 → Recovery).

**Smell test before declaring a SendMessage dispatch "delivered":**

> Did the recipient produce a new JSONL snapshot AFTER the SendMessage timestamp? If not after their role threshold, the wake-hook may have dropped — initiate HO-direct recovery rather than continuing to wait.

## Cross-references

- **T-0216** (`sendmessage-without-spawn-dispatch-failure.md`): the prior-discovered SendMessage failure mode (orphan inbox, target not in members). Distinct mechanism; both share the `success:true` returns ≠ delivered confusion.
- **T-0223** (commit + LIVE 2026-05-11): activity-monitor with P1.a (HO-state) + P1.b (queue-pullable) predicates. These cover the legitimate HO-absent + queue-existing case (cap-hit, between-sessions).
- **T-0225** (filed + landed + backed out 2026-05-11/12): P1.c worker-silence predicate. Reverted per the operator verdict msg_1326 — the state it was detecting isn't a real steady state. See T-0228.
- **T-0228** (Recovery-target): back-out ticket for T-0225 P1.c.
- **ADR ORG-011** (commit 6529e12): "HO-absent-operator-keep-working" Phase B coverage. The active-team smell-test is the discipline-tier sibling; P1.a + P1.b are the structural sibling. P1.c was removed mid-arc as not corresponding to a real state.
- **`feedback_active_team_worker_check.md`** (new memory pin): HO end-of-turn smell-test for active-team worker silence. The primary mitigation for this bug.
- **`feedback_drift_to_passive_smell_test.md`**: HO-side discipline of status-checking workers after silence threshold. Pairs with the active-team check.

## Discovered

- **2026-05-11 b36 cycle** (T-0206 kino chat image handling). Unit 3 implementer SendMessage to team-lead at 18:08:10Z returned `success:true`; orchestrator never woke until HO status-check at 23:33:36Z (5h27m later). HO bypassed the orchestrator and spawned reviewer-b36-u3 directly; orchestrator re-engaged via the wake-up status-check and completed the cycle.
- Evidence in transcripts at `~/.claude/projects/-home-claude/19b2f769-bcfc-4438-ad66-937221bf9ca8/subagents/`:
  - Implementer `agent-a17cabf5d8dd7b75a.jsonl` (last alive at 18:08:19Z, SendMessage at 18:08:10Z).
  - Orchestrator `agent-a5110a1dccb2ee6c4.jsonl` (last alive at 18:00:24Z, "Idle pending implementer's READY").
  - Orchestrator post-wake `agent-a280a5721501a52ec.jsonl` (first new turn at 23:33:36Z, explicit statement "I have no record of receiving Unit 3 READY").
- **T-0226** filed 2026-05-11T23:43Z, resolved 2026-05-12 with this bugs/ entry. Resolution adopted T-0225's P1.c predicate as the mitigation (detection-only; no Pantheon-side delivery-layer fix is available).

- **2026-06-10 echo-arc-w24 cycle** (T-1075 ADR-221 draft phase). Bidirectional-confusion variant: implementer's plan-ready SendMessage to orchestrator (~22:18Z) returned success but never woke the orchestrator; orchestrator meanwhile concluded its own assignment had dropped and burned a wake-check + full re-send on the implementer (who had received the original fine). Both tiers waited on each other ~30 min until HO crossed the gap with direct HO->worker messages (reliable path held: all four HO->worker and worker->HO-notification deliveries in the same window landed). Mitigation refined mid-arc: orchestrator briefed to treat >10-min silence after a reply-expecting SendMessage as check-the-counterpart's-inbox-file (or ask HO to relay), never as evidence of non-receipt. The deadlock shape is worse than the b36 single-direction case because each side's recovery action (re-send / wait harder) reinforced the other's misdiagnosis.

- **2026-06-10/11 echo-arc-w24 addendum — queue-replay poisoning + the fresh-context cure.** Follow-on from the same cycle's bidirectional drops: once a worker's inbox accumulates contradictory instructions (assignment → lock → reversal → freeze), wake-recovery makes it WORSE — the worker drains the queue across turns, acting on each stale instruction in sequence (committed a wrong-design revision, then a redundant rewrite, violating an explicit freeze twice). Precedence-anchor messages ("this supersedes everything earlier") cannot help: they sit at the queue tail while the worker acts on the head. Verified working cure: terminate the contaminated worker (shutdown_request) and stage a fresh-context replacement with the current ground truth in its spawn brief; the replacement executed cleanly first-try (T-1068 1-REBOUND cycle + ADR-222 same hour). Secondary mitigations that held: single-driver rule for shared mutable state (HO performs the git fix itself rather than racing instructions through two tiers), freeze-before-surface (freeze the artifact BEFORE sending it up for ratification, adopted by orch for ADR-222), and check-counterpart-inbox-after-10-min-silence instead of re-sending.

- **2026-08-12 T-2652 broker arc (T-2909/AB-1 dispatch).** Highest observed drop density: the PO's implementer completed its plan TWICE (original + explicitly-verbatim re-send) and neither completion woke the PO — both surfaced instead as task-notifications at the SESSION ROOT (HO), which is itself a useful signal: **a sub-subagent's completion notification routes to the session root when the intermediate tier's wake drops**, so the HO sees the stall in real time and can relay-nudge. Directed HO→agent sends stayed 7/7 reliable in the same window (consistent with every prior instance). Two new verified recoveries: (a) **transcript extraction over re-request** — the PO read the implementer's final assistant message directly from its on-disk JSONL with a bounded line-parser (collect assistant text blocks, print the last), getting the plan verbatim with zero reconstruction risk and no second round-trip; (b) **harness-tracked silence watchdog** — HO armed a background mtime-watch on the PO's inbox file (activity ⇒ exit 0, N-min silence ⇒ exit 1; either way the task-completion wake re-invokes HO), converting "hope the wake fires" into a bounded alarm without foreground sleeps.

## Keywords

pantheon, sendmessage, success-true-but-recipient-silent, wake-hook-flaky, intermittent, team-pattern, teammate-message-inject, worker-state-desync, idle-worker, b36, t0206, t0226, claude-code-harness, dispatch-pattern, recipient-wake-failure, activity-monitor-mitigation, p1c-worker-silence, smell-test-dispatch
