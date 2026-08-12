---
symptom: "Agent turn ends silently mid-response after 'Server is temporarily limiting requests - Rate limited' - no retry, no log entry, no operator signal"
fix: "activity_monitor.py P1.d predicate detects it via capture-pane plus the isApiErrorMessage/rate_limit jsonl flags and retry-injects to resume the abandoned turn"
domain: anthropic-api, rate-limit, agent-liveness, silent-failure
---
# Anthropic API rate-limit aborts an agent's turn silently — no retry, no signal

## Symptom

Agent (Orchestrator / Recovery / Specialist) receives a user message; starts to respond; one of the agent's tool/inference calls returns:

```
API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited
```

After this error the turn ENDS. No retry. No log entry visible to the operator. No subsequent action.

The session sits idle until the next user message arrives. The user perceives unresponsiveness; nothing in the agent's pane or in any Pantheon health probe signals that anything's wrong.

Distinct from the **5h-cap-hit** class (which fires `StopFailure` hook → ho_resumer.py recovers). The rate-limit class is a server-side throttle, not a usage-window cap; it leaves no `StopFailure` event.

## Confirmed instance

2026-05-12 13:07:56Z — the operator "Gm" → Orchestrator tried to ToolSearch for the reply tool → got `Rate limited` API Error → turn ended. Orchestrator sat idle 44 min until the operator's second "gm" at 13:52:01Z. Same session jsonl: `/home/claude/.claude/projects/-home-claude/113e2a36-7808-4d99-963f-0cbe843efdef.jsonl`. See msg_1119 / msg_1122 / msg_1123.

## Why current probes miss it

- `check_orchestrator_health` (inbox_consumer.py): probes bridge `telegram_connected` + `connected_clients` + queue stall + pane wedge sentinel. Bridge is green; no queue; no wedge. All four signals pass while Orchestrator is silently aborted.
- `activity_monitor` (T-0223 P1.a): fires only when agent's commit/ledger/jsonl is silent > 1hr. Orchestrator's jsonl HAS recent entries (the rate-limit error itself was written), so jsonl mtime is fresh — P1.a doesn't fire even though Orchestrator is functionally dead.
- `activity_monitor` (P1.b): requires the last assistant turn to match an `await-state` regex (waiting on / pausing on / etc.). The rate-limit error message doesn't match, so P1.b doesn't fire either.

## Detection signal

JSONL-scan probe with the shape:

```
latest_user_turn_ts = ts of most-recent type=user message in agent's jsonl
latest_assistant_turn_ts = ts of most-recent type=assistant entry
latest_assistant_text = text content of most-recent assistant entry

if latest_assistant_turn_ts > latest_user_turn_ts
   AND latest_assistant_text matches /API Error.*[Rr]ate limit|temporarily limiting requests/
   AND (now - latest_assistant_turn_ts) > threshold (e.g., 5 min):
    → AGENT IS RATE-LIMITED AND ABANDONED THE TURN
    → fire a poke (similar to activity_monitor P1.a) OR a retry inject ("Retry your previous response.")
```

The retry-inject path is more useful than a poke: send-keys "Retry the response you were composing before the rate-limit error." into the pane. Resumes the abandoned turn.

## Recommended fix — LANDED

Extended `tools/activity_monitor.py` with a P1.d predicate. Per the operator msg_1125, capture-pane is primary signal (the pane reflects what the user sees); jsonl-scan is used only to time staleness via the structured `isApiErrorMessage` + `error: "rate_limit"` flags. Cadence shares the existing 15-min timer; per-predicate cooldown is 15 min (vs P1.a/P1.b's 30 min — rate-limit retries should be re-eval'd faster). Action is retry-inject ("Retry the response you were composing..."), not just a poke — resumes the abandoned turn directly.

Landed as T-0254 / activity_monitor.py P1.d.

## Related bugs / tickets

- `bugs/sendmessage-without-spawn-dispatch-failure.md` — adjacent class of silent dispatch failure (different mechanism; same impact shape).
- T-0223 (ORG-011 Phase B activity_monitor parent) — extension point.
- T-0142 + T-0201 (auto-resolve allowlist + Recovery alert plumbing) — pattern this fix would slot into.

## Cluster tracking

The operator msg_1122 ("not the first rate limit that's messed things up") confirms this class is recurring. Per `sop/standard/handling-low-level-errors.md` Tier 2: if cluster-detection cron lands + threshold fires for this class, surface as architectural ticket — likely "agent runtime should auto-retry rate-limit errors with backoff" (vendor SDK feature request or Claude Code wrapper layer).
