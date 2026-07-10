---
aliases: [ORG-007]
adr-id: ORG-007
title: kino ↔ claude wire protocol — stream-json long-running process per agent
status: Ratified
date: 2026-05-07
author: orchestrator (Orchestrator)
hitl: Ratified
ratified_by: operator
ratified_via: telegram (msg_903, 2026-05-07)
related:
  - adr/ORG-006-kino-pwa.md
  - knowledge/episodic/2026-05-07-kino-claude-wire-protocol/notes.md
  - tickets/active/T-0111.md
  - tickets/active/T-0113.md
  - rulebook/principles.md (P1, P5)
  - rulebook/directives.md (D1, D3)
---

# ORG-007 — kino ↔ claude wire protocol

**Status:** Ratified 2026-05-07 by the operator (Telegram, msg_903). HO-authored 2026-05-07 under autonomy-bar D3 + D2; the operator's ratification answers fold inline below in §Ratification answers (replacing prior §Open questions). T-0113 implementation arc unblocks against this ratified ADR.

**Authoring context:** kino Phase 2 supervised first-fire (2026-05-07) surfaced a wire-protocol gap. ORG-006 §Per-agent identity model locked `claude binary in stdio-MCP mode` as the agent backend without specifying the wire protocol layer between kino-daemon and the running claude process. POST `/api/agents/:id/send` writes `{content, session_id}` JSON to the agent's IPC socket; `claude mcp serve` speaks MCP JSON-RPC 2.0 only, silently dropping non-JSON-RPC payloads. Researcher dispatch (`knowledge/episodic/2026-05-07-kino-claude-wire-protocol/notes.md`) enumerated 7 options against ORG-006's binding constraints. This ADR ratifies the synthesis call.

## Decision

**The kino-spawned per-agent process runs `claude -p --input-format stream-json --output-format stream-json --verbose --allowedTools <permissive-bounded-list>` instead of `claude mcp serve`.** It is a long-running process per agent, accepting NDJSON user-turn frames on stdin and emitting NDJSON event sequences on stdout. The socat ACCEPT-FD bridge from ORG-006 §Phase-1-implementation-addendum is unchanged.

The exact `--allowedTools` value is defined at T-0113 plan-time (see §Ratification answers Q2); the posture is permissive-but-bounded, matching the existing interactive permission surface that Orchestrator/Specialist/Recovery run today, NOT the more aggressive `--dangerously-skip-permissions` stance.

### Wire format (input — kino-daemon writes per user turn)

```
{"type":"user","message":{"role":"user","content":"<user-typed text>"}}\n
```

One NDJSON line per user message. The next line is not written until the prior turn's `{type:"result"}` event has been read on stdout (queue is one-turn-at-a-time per agent — `RequestResponseQueue` already serializes per `subprocess_id`).

### Wire format (output — kino-daemon reads after each user turn)

```
{"type":"system","subtype":"init","session_id":"<uuid>","tools":[...],...}    # only on process start
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."}]},...}
{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"<token>"}}, ...}    # repeated, when --include-partial-messages
{"type":"result","subtype":"success","result":"<final text>","session_id":"<uuid>","num_turns":N,...}
```

The terminal frame for each turn is `{type:"result"}`. kino-daemon resolves the queue entry on receipt of the result frame.

### Session identity

kino-daemon pre-generates a UUID per agent at registration time and passes it to the claude process via `--session-id <uuid>` at process spawn (not capture-from-init). The UUID is stored in the SQLite `agent_state` table per agent. On agent process restart (crash, SIGHUP, etc), kino-daemon respawns the agent with `--session-id <stored-uuid> --resume` so conversation context is preserved across restart cycles.

### Streaming to PWA WebSocket

Optional. With `--include-partial-messages` flag, kino-daemon receives `stream_event` text-delta frames. kino-daemon may forward these as partial WebSocket frames to the PWA pane, enabling token-level streaming render in the AgentPane. v0 implementation may defer streaming and only render on `result` frame; streaming is a Phase-2.5 polish item, not a Phase-2-close blocker.

### Permission posture

v0 uses `--allowedTools <permissive-bounded-list>` per ratification Q2 (the operator, Telegram msg_905 correction): "no one is running `--dangerously-skip-permissions` flag at the moment." The kino-spawned per-agent process runs non-interactively (no TTY for workspace-trust dialog or per-tool permission prompts), but the explicit allowlist captures the agent's trust surface without granting blanket skip-all-permissions authority. Reference baseline: Orchestrator/Specialist/Recovery's existing interactive permission surface (`~/.claude/settings.local.json` for Orchestrator + analogous per-user for specialist/recovery) — T-0113 implementer cross-references this at plan-time and proposes the exact `--allowedTools` argument list; HO ratifies the specific list at plan-review.

The `--dangerously-skip-permissions` flag is explicitly NOT used. If the trust posture later tightens further, narrow the allowlist; if it loosens, surface as a fresh ratification (do NOT silently switch to skip-permissions).

## Decision rejection summary

The 7-option research dispatch identified six alternatives. Each is rejected here for reference; full analysis in `knowledge/episodic/2026-05-07-kino-claude-wire-protocol/notes.md`.

- **Option 1 (MCP tools/call framing):** infeasible. `claude mcp serve` exposes 30 tools (Bash, Read, Edit, agent-coordination, etc) but NONE trigger conversational LLM response. Confirmed by live probe. Reliance on `sampling/createMessage` is not viable — `claude mcp serve` does not implement it as a server feature (GitHub issue #1785).
- **Option 2 (claude --print one-shot per message):** viable fallback. Each user message spawns a fresh claude process. Documented + proven, but ~1-3s startup latency per message. Adopted only if Option 3's input schema proves unstable.
- **Option 4 (kino-as-bridge-server):** structural non-starter. Reintroduces the bridge's identity-collapse failure mode that ORG-006 explicitly rejected. Do not pursue.
- **Option 5 (claude --print --resume per message):** Option 2 variant with streaming output. Same startup-latency cost; same conditional adopt-if-Option-3-unstable fallback.
- **Option 6 (Agent SDK in-process):** violates ORG-006 per-agent OS-level identity (all agents would run as kino user). Future-state option only if the per-agent systemd subservice architecture is revisited; not v0.
- **Option 7 (kino-agent shim process):** secondary if kino-daemon's socket protocol is constrained. As kino-daemon is in active development, modifying it directly is cheaper than adding a shim layer. Do not pursue.

## Justifies via

- **P1 (architecture over policy):** stream-json is the official Agent SDK transport (vendor doc: code.claude.com/docs/en/headless), not a workaround. Process-per-agent + stable session IDs are structural; identity correctness is enforced at the OS level (User=%i on the systemd subservice from ORG-006 Deviation 1) + the wire-protocol level (each process owns one agent's session).
- **P5 (separation of orchestration and execution):** kino-daemon stays the comms substrate (queue, IPC, WebSocket); claude handles execution (tools, LLM turns, session state). The wire-protocol boundary is the seam.
- **D1 (the operator's bandwidth):** the pattern works structurally — once Option 3 is wired, the operator's send-message → receive-response loop in the PWA is closed without per-message intervention.
- **D3 (autonomy bar):** ADR-tier ratification gate is explicit. HO authored under D3 + D2; the operator ratifies on the 5 open questions before T-0113 implementation arc fires.

## Implementation outline (T-0113 scope when ratified)

1. **`scripts/systemd/kino-agent@.service.example` ExecStart**: replace `claude mcp serve` with `/home/%i/.npm-global/bin/claude -p --input-format stream-json --output-format stream-json --verbose --allowedTools <permissive-bounded-list-from-plan> --session-id ${KINO_AGENT_SESSION_ID}` (or equivalent — `--session-id` injection mechanism + exact `--allowedTools` list are part of T-0113 plan).
2. **kino-daemon socket writer**: change payload from `{content, session_id}` to `{"type":"user","message":{"role":"user","content":"<text>"}}\n`. NDJSON, one line per user turn.
3. **kino-daemon socket reader**: parse NDJSON lines from socket. Capture `system/init` event's `session_id` (sanity-check matches the `--session-id` we passed). On `{type:"result"}`, call `queue.route()` to resolve pending request with the final text. Optional: forward `stream_event` text-delta frames to the agent's WebSocket connection for streaming render.
4. **Session ID lifecycle**: kino-daemon pre-generates `agent_state.session_id` UUID per agent at registration time; passes to ExecStart via Environment= directive on the systemd unit; respawn after crash uses the same UUID + `--resume`. SQLite `agent_state` table holds the mapping.
5. **Tests**: `kino_writes_stream_json_user_frame_to_socket`, `kino_resolves_queue_on_result_frame`, `kino_handles_partial_messages_for_streaming`, `kino_resumes_session_after_agent_restart`. Brief-named per cycle convention.

Acceptance: PWA send-path message routes to claude → conversational response renders in the AgentPane. Supervised first-fire smoke with the operator (Phase 2.6 / ORG-007 close).

## Ratification answers (the operator, Telegram msg_903, 2026-05-07)

**Q1 — Input schema stability vs fallback commitment: Option 3 alone, no coded fallback.** T-0113 commits to `--input-format stream-json` directly. If the schema breaks (vendor change), surface as a separate ticket with breakage-response design then. Don't pre-bake Option 2 fallback paths now.

**Q2 — Permission posture: allowlist via `--allowedTools`, not `--dangerously-skip-permissions`.** Use a permissive-but-bounded allowlist. Reference baseline: "what all 3 of you should currently have" — i.e., the existing interactive permission surface for Orchestrator + Specialist + Recovery (broadly visible at `~/.claude/settings.local.json` for Orchestrator + analogous for the other two unix users). T-0113 implementer plans the exact `--allowedTools` argument list at plan-time, cross-referencing the existing surface; HO ratifies the specific list at plan-review.

**Q3 — Crash-recovery: prefer `--resume`** (preserve conversation context across crashes/restarts). kino-daemon stores `{agent_id → session_id}` in the SQLite `agent_state` table; on agent process crash, respawn with `--resume <stored_session_id>` so the conversation continues where it left off rather than starting fresh.

**Q4 — Streaming UX: streaming-as-implementation-signal OK; raw-token-rendering REJECTED.** Per the operator's clarification (msg_905): "from an implementation point, streaming may be an easier thing to use to indicate typing (i.e. stream starts, code logic shows bubble until stream finishes) — it's allowable if the implementation is helped by it. But from the ux side, seeing tokens stream in on the ui is ugly."

Implementation guidance: kino-daemon MAY use the underlying stream-json output frames as a timing source for the typing indicator (stream-starts → emit typing-on frame to PWA WebSocket; stream-finishes / `{type:"result"}` → emit typing-off frame + full final message render). The PWA does NOT render raw token deltas as text in the AgentPane; it renders the typing-bubble (like messaging apps) during the stream and then renders the complete final message in one append on result-frame. This separates the *transport* layer (streaming OK) from the *render* layer (no raw-token-streaming-display).

T-0113 plan should specify whether the typing indicator's start/stop is driven by stream-event partial frames (cheaper) or by an explicit "agent is processing" daemon-side flag (simpler); both are acceptable per ratification.

**Q5 — Option 6 (Agent SDK in-process): rejected outright, NOT noted as future-state.** "Keeping agents separate was a choice made with good reason" (the operator). Per-agent OS-level identity is non-negotiable; Option 6's in-process pattern violates that and is structurally off the table. ORG-007 does NOT include a §Future-state gate for it. If future technical evolution makes a different in-process pattern viable, it'd be a fresh ADR with explicit re-ratification — not a pre-authorized fallback in this ADR.

## Related

- **`adr/ORG-006-kino-pwa.md`** — kino PWA design + Phase-1-implementation addendum. ORG-007 specifies the wire-protocol layer that ORG-006 §Per-agent-identity-model left underspec'd. ORG-006 stays Ratified; ORG-007 extends.
- **`knowledge/episodic/2026-05-07-kino-claude-wire-protocol/notes.md`** — full 7-option enumeration, sources, empirical confirmations.
- **`tickets/active/T-0111.md`** — Phase 2 Cycle 5 (build integration + supervised first-fire); the wire-protocol gap surfaced at first-fire (msg_889).
- **`tickets/active/T-0113.md`** — implementation arc (unblocked + active post-ratification 2026-05-07).
- **`bugs/systemd-socket-activation-socat-acceptfd-bridge.md`** — the IPC layer fix from Phase 1b (still load-bearing — ORG-007 wire protocol rides on top of socat ACCEPT-FD).
- **`pantheon-pwa/src/server/routes.ts`** — current send-path that needs the payload-shape change.
- **`pantheon-pwa/scripts/systemd/kino-agent@.service.example`** — current ExecStart; T-0113 modifies.
- **External:**
  - [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) — flag specs.
  - [Run Claude Code programmatically (headless)](https://code.claude.com/docs/en/headless) — stream-json examples.
  - [stream-json input schema gap (GitHub #24594)](https://github.com/anthropics/claude-code/issues/24594) — undocumented format risk.

## Ratification

Ratified 2026-05-07 by the operator (Telegram msg_903) with answers to all 5 open questions folded inline above in §Ratification answers. Authored 2026-05-07 by HO (Orchestrator) under autonomy-bar D3 + D2. Implementation arc:

1. T-0113 (kino wire-protocol implementation) unblocks against this ratified ADR.
2. ExecStart change to `kino-agent@.service.example`: `claude -p --input-format stream-json --output-format stream-json --verbose --allowedTools <permissive-bounded-list-from-plan>`. NO `--dangerously-skip-permissions`.
3. Streaming UX: typing-indicator bubble on agent processing, full message render on `{type:"result"}`. NO raw token streaming.
4. Crash-recovery: SQLite `agent_state.session_id` per agent, respawn with `--resume` on crash.
5. Per-agent OS-level identity preserved per ORG-006 §Per-agent-identity-model + ORG-006 §Phase-1-implementation-addendum (per-agent systemd subservices + socat ACCEPT-FD bridge from Deviation 2 stays unchanged).
