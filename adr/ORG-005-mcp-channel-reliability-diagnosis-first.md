---
aliases: [ORG-005]
title: "MCP channel reliability: diagnosis-first fix path"
status: Ratified 2026-05-04
date: 2026-05-04
# re_evaluate: machine-checkable mirror of this ADR's inline re-eval prose,
# consumed by tools/adr_reeval_walk.py (T-2527). Backfilled per T-2530.
re_evaluate:
  - id: R1
    type: event
    when: "the trust-posture re-eval cycle runs"
    fires: surface
    text: "the two dispatch-discipline patterns (ratified-design-vs-current-priority drift; symptom-as-design-input vs root-cause-first) fold into feedback memory and re-evaluate when the trust-posture re-eval cycle runs"
build_disposition: built
---
# ORG-005 — MCP channel reliability: diagnosis-first fix path

**Status:** Ratified v2 2026-05-04 by the operator (Telegram, msg_448 — "Path A"). v1 superseded in-place post-Recovery-correction.
**HITL:** Ratified.
**Date:** 2026-05-04.
**Justifies via:** P1 (architecture over policy — fix root cause, not symptom), P3 (decisions justify upward), P4 (traceability — record why we chose what we chose, per msg_442).

## ⚠️ Correction (v1 superseded)

The v1 of this ADR (committed at 41b58e0) named H2 (cross-instance collision, Orchestrator + Recovery both polling the same bot) as the confirmed root cause and recommended disabling Recovery's `claude-telegram-fallback.service`. Recovery provided cross-user `.env` reads showing **three distinct bot tokens** (Orchestrator=`8672155716`/`c_swarm_bot`, Recovery=`8279653463`/`c_backup_bot`, Specialist=`8604098966`/`c_specialist_bot`) — H2 is structurally impossible (cannot collide at the bot layer with different tokens). The v1 diagnosis dispatch could not read Recovery's `.env` (cross-user perms blocked it) and inferred "same token" from one-side evidence; HO failed to cross-check against memory `project_telegram_two_bot_topology.md` which already documented "different bot from prime." The Findings + Decision sections below are revised. v1's wrong-but-historical sections are preserved verbatim in `knowledge/episodic/2026-05-04-mcp-channel-reliability-diagnosis/notes.md` under the §Correction banner for audit-chain integrity.

## Context

The Telegram MCP plugin loaded into Orchestrator's HO session periodically loses its bot lock — the `mcp__plugin_telegram_telegram__*` tool surface vanishes mid-session, in-flight `reply` calls fail, inbound messages stop delivering. The handoff captured this as "MCP eviction recurred multiple times this session during subagent dispatches" and treated it as a known cost-of-doing-business. The operator's actual position (msg_439): "We need channels that don't drop. If the dropping is an error in local config, then we need to fix that."

A misalignment surfaced 2026-05-04 around how to fix it. ORG-004 (ratified 2026-05-02, MCP transport bridge) framed Phase 1 as building a Telegram-specific bridge process. When the operator said "start on ORG-004" (msg_422) + "make a git project for it" (msg_423), HO executed Phase 1 — built `pantheon-bridge` as a working Python service (T-0025 done; 73 tests green; live smoke clean). The operator then surfaced that the entire endeavor was misdirected (msg_438): "I did not want to replace the telegram plugin with another telegram solution. I want the mcp issues gone." His original mental model was the Phase 2 deliverable (web UI passthrough to CLI), not Phase 1's Telegram bridge. The ratified ORG-004 design's Phase ordering didn't match the operator's actual priority.

Then the further refinement (msg_439): channels that don't drop is the actual goal; the bridge / web-UI options are both workarounds. **Diagnose first; fix the config if the cause is config; only build a workaround if the cause is structural.** That's the framing this ADR captures.

## Investigation

A focused diagnosis dispatch (general-purpose agent, read-only) ran 2026-05-04 against five hypotheses. Full notes at `knowledge/episodic/2026-05-04-mcp-channel-reliability-diagnosis/notes.md`. Method: read `~/.claude/settings.json` + plugin manifest + `.mcp.json`, read the plugin's `server.ts` end-to-end, enumerate `bun server.ts` processes via `pgrep`/`ps -ef`, trace parent chains via `pstree`, inspect systemd unit files, probe the Bot API directly with `curl`, search transcripts for eviction-shaped events.

## Findings (v2 — revised post-Recovery-correction)

The Telegram MCP plugin eviction is **intra-process-tree, intra-account** — not cross-instance. Three Claude installations on this VPS each run a different bot:
- Orchestrator (`claude` UID, `c_swarm_bot` token prefix `8672155716`) — interactive HO session
- Recovery (`claude-fallback` UID, `c_backup_bot` prefix `8279653463`) — recovery
- Specialist (`claude-research` UID, `c_specialist_bot` prefix `8604098966`) — Specialist

Different bot tokens cannot collide at Telegram's `getUpdates` layer. The actual mechanism is the one ledger entry `2026-04-29T21:31:00Z-001` already documented (Issue `anthropics/claude-code#46617`): in Claude Code v2.1.101+, **any Claude Code session that loads the Telegram plugin SIGTERMs whoever previously held the bun server for that user account**. Subagent dispatches inherit `enabledPlugins.telegram=true` from the parent's user-scoped settings (per-agent scoping is not supported per `claude-code-guide` research in the existing audit chain); the subagent's CC subprocess loads the plugin → spawns a fresh `bun server.ts` under the same UID → SIGTERMs Orchestrator's bun. From Orchestrator's view, the `mcp__plugin_telegram_*` tool surface vanishes from the deferred-tools list. The "eviction during subagent dispatches" correlation in the handoff was the actual signal of causation, not — as the v1 dispatch claimed — a downstream artifact of cross-instance competition.

Hypotheses (corrected verdict):
- **H1 (subagent dispatch spawns competing poller in same process tree) — CONFIRMED.** The dispatch's v1 disconfirmation rested on insufficient evidence; the audit chain's Issue #46617 entry is the load-bearing source.
- **H2 (cross-instance collision) — DISCONFIRMED.** Three distinct tokens; no shared poll lock.
- H3 (plugin reconnect cycle) — disconfirmed; no internal periodic restart.
- H4 (process-lifecycle event) — disconfirmed; Specialist is curl-direct outbound.
- H5 (bun-runtime instability) — disconfirmed; both pollers stable for hours/days.

## Decision (v2 — revised)

The fix is structural, not config-only. The eviction is a Claude Code architectural artifact (single-bun-per-account, no per-agent plugin scoping) compounded by the plugin's process-launch model. There is **no local config Orchestrator can change to prevent it**, because:
- `enabledPlugins.telegram` is user-scoped, not session-scoped or agent-scoped (Issue `anthropics/claude-code#1594` — daemon-mode plugin transport — is the open upstream that would change this; no ETA).
- Disabling it for the Orchestrator user kills Telegram entirely for the parent session, not just for subagents.
- "Don't dispatch subagents" is not a workable constraint; subagent dispatches are core HO mechanism.

**Two structurally-correct paths surface:**

**Path A — ORG-004 reactivation (Telegram-specific bridge, already built).** The pantheon-bridge built earlier today as T-0025 owns the bot connection persistently and serves MCP over HTTP/SSE. Claude sessions (parent + subagents) connect as HTTP clients without spawning their own bun. This is precisely the failure mode the original ORG-004 design addresses; the corrected diagnosis matches the design's stated rationale. T-0026/27/28 chain unblocks; cutover via per-instance MCP-config flip.

**Path B — Web-UI passthrough to CLI (the operator's stated preference, msg_438).** Sidesteps MCP entirely. Web UI accepts messages from phone/laptop, pipes to a running `claude` CLI session (or invokes `claude -p` per-message), streams responses back. Solves not just Telegram-eviction but all MCP-shape problems generically. New scope; pantheon-bridge becomes a built artifact whose architecture-pattern can inform the web UI's transport but whose code does not directly carry over (different protocol entirely). Telegram either retires or stays as a parallel push-notification channel.

**Path C — Wait for upstream Issue #1594.** Cheap; might never land; not actionable as a current decision.

**Recommendation surfaced for the operator's ratification (HO does not auto-pick between Path A and Path B):** the corrected diagnosis makes ORG-004 *technically fit* the failure mode, but the operator's msg_438 stated direction was Path B (web-UI passthrough — "I want the mcp issues gone"). Path B is bigger scope (a new build) but eliminates the entire MCP-shape risk surface; Path A is smaller scope (the bridge is built and tested) but ships another MCP-shape solution that future MCP-architecture changes could undermine. **Decision is operator-tier (D2 — orchestrator-with-explicit-ratification on direction-of-travel choices); HO surfaces both paths with tradeoffs and awaits the operator's pick.**

**the operator ratified Path A (msg_448 "Path A"):** reactivate ORG-004. The bridge built earlier today (T-0025, github.com:example-org/pantheon-bridge) carries forward as the structural fix. T-0026 unblocks (Recovery target — systemd unit on Orchestrator's host); T-0027 + T-0028 stay blocked on the chain (cutover then decommission). Web-UI passthrough is not pursued in this arc; Path B remains a future option if Path A's ORG-004 lifecycle surfaces additional MCP-shape pain that the bridge can't address.

## Consequences (v2)

### For ORG-004 (Telegram bridge)

Status depends on the operator's Path A vs Path B pick:
- **Path A picked:** ORG-004 reactivates. T-0026/T-0027/T-0028 unblock; the bridge becomes the structural fix. ORG-004 Phase 2 (web-UI on top of bridge's session API) becomes the longer-tail follow-on per the original phased design.
- **Path B picked:** ORG-004 Phase 1 is superseded by Path B. The bridge code at `github.com:example-org/pantheon-bridge` becomes an artifact (kept dormant or archived). T-0025 stays resolved (the build was real and clean); T-0026/27/28 transition to a permanently-shelved equivalent of Done with reason "superseded by web-UI passthrough."

### For Telegram as a channel

- **Path A:** Telegram stays as the primary operator channel; bridge serves it more reliably.
- **Path B:** Telegram either retires or stays as a parallel push-notification channel (web-UI is primary; Telegram is the away-from-laptop fallback).

### For the dispatch discipline

Three patterns to fold into the feedback memory + re-evaluate at the trust-posture re-eval cycle:

1. **Ratified-design vs. current-priority drift.** A ratified design captures intent at ratification time; it can become stale as priorities evolve. When a deferred design unblocks, HO should re-check the framing against the operator's current statement of the problem before executing.

2. **Symptom-as-design-input vs. root-cause-first.** The original ORG-004 framing baked "MCP eviction is structural" as a premise without diagnosis. A diagnosis dispatch *before* the design ticket would have produced the corrected H1-confirmation cheaper. Future structural designs around an observed failure should include a diagnosis-first checkpoint.

3. **Subagent-research findings must cross-check against memory + ledger before ADR.** This v1 ADR's central claim ("same bot token") was inferred-not-observed by the dispatch; existing memory `project_telegram_two_bot_topology.md` already documented the contradiction. HO committed the inference to ADR without cross-checking. This is the second time today an unchecked subagent-produced artifact slipped a wrong claim past HO into a decision document. **Pattern flagged for a feedback memory; structural backstop options to evaluate: a pre-commit-on-ADR ritual that requires HO to enumerate "memory + ledger entries that could falsify this claim" before ratify-stage.**

## Re-evaluation triggers

- **If Path A picked + ORG-004 reactivated:** post-deployment observation period (1–2 weeks). Count `mcp__plugin_telegram_*` tool-surface drops in HO transcripts. Target: zero per session.
- **If Path B picked:** new ADR for the web-UI passthrough scope. ORG-005 closes once Path B's ADR ratifies.
- **If Issue #1594 (daemon-mode plugin transport) lands upstream:** revisit the picked path. The new transport may obviate Path A's bridge entirely; would not affect Path B (which is MCP-agnostic by design).

## Related

- `knowledge/episodic/2026-05-04-mcp-channel-reliability-diagnosis/notes.md` — full diagnosis (1382 words).
- `adr/ORG-004-mcp-transport-bridge.md` — the (now-paused) structural workaround.
- `tickets/done/T-0025.md` — bridge build (shipped but not deployed).
- `tickets/blocked/T-0026.md` — systemd unit (blocked pending this ADR's outcome).
- `tickets/blocked/T-0027.md` + `T-0028.md` — cutover + decommission (chained-blocked).
- `github.com:example-org/pantheon-bridge` — the bridge repo artifact.
- Telegram msg_422 (start ORG-004) → msg_438 (misalignment surfaced) → msg_439 (fix-config-first framing) → msg_442 (capture in ADR).
