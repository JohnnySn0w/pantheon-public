---
aliases: [ORG-004]
adr-id: ORG-004
title: MCP transport bridge for fleet messaging (Telegram now; web UI later)
status: Phase 1 closed-as-abandoned; Phase 2 superseded by ORG-006; bridge daemon + repo DECOMMISSIONED 2026-06-04 (T-0767)
date: 2026-05-02
author: orchestrator (Orchestrator)
hitl: Ratified (original); superseded 2026-05-05; decommissioned 2026-06-04
decommissioned_via: T-0767 (2026-06-04 — HO migrated off the bridge to per-agent c_swarm_bot plugin self-poll; bridge user-unit disabled, example-org/pantheon-bridge repo archived)
ratified_by: operator
ratified_via: telegram (msg_299 sequencing + msg_333 ratification + prior-art directive)
superseded_for_phase_2_by: ORG-006-kino-pwa
phase_1_closed_via: T-0091 (strategic-pivot directive 2026-05-05) + T-0073 (resolved-as-abandoned)
related:
  - /tmp/architecture-handoff/transports-analysis-2026-05-02.md
  - knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md
  - rulebook/principles.md (P1, P3)
  - rulebook/directives.md (D1)
  - adr/ORG-006-kino-pwa.md (supersedes Phase 2)
  - tickets/done/T-0091.md (Phase 1 abandonment directive)
  - tickets/done/T-0073.md (Phase 1 cutover closed-as-abandoned)
---

# ORG-004 — MCP transport bridge for fleet messaging (Telegram now; web UI later)

> **2026-06-04 decommission (T-0767):** The bridge daemon is retired. HO/Orchestrator — the bridge's last and only consumer (Specialist/Recovery always self-polled their own bots) — was migrated to the per-agent official-plugin self-poll model on its own bot (`c_swarm_bot`, `--channels plugin:telegram@claude-plugins-official`), reaching transport/identity parity with Specialist/Recovery and eliminating the SSE competing-client failure class (T-0719/T-0759) for HO. Post-cutover: `pantheon-bridge.service` (claude user-unit) disabled + inactive; `example-org/pantheon-bridge` repo archived. ORG-004 is preserved (not retracted) — its structural lessons remain load-bearing for ORG-006. This ADR is now fully historical: both phases are closed/superseded and the daemon no longer runs anywhere in the fleet.

> **2026-05-05 status update:** Phase 1 was implemented and deployed across the fleet (T-0073 fan-out: T-0078 stdio wrapper, T-0087 Specialist cutover, T-0088 Recovery cutover, Orchestrator cutover). It was declared a nonstarter the same day (T-0091, the operator directive 2026-05-05) due to a structural failure: single `BOT_TOKEN` + chat_id→single-target routing meant all agent outbound consolidated onto one bot identity. Phase 2 (kino PWA) is superseded by **ORG-006-kino-pwa.md** (ratified 2026-05-05), which redesigns the comms substrate around per-agent stdio MCP subprocesses with OS-level identity enforcement. ORG-004 is preserved (not retracted) — its structural lessons are load-bearing for ORG-006's design. Cleanup of bridge daemon + repo is tracked at T-0093 (depends_on: T-0074).

**Status:** Ratified 2026-05-02 by the operator (Telegram, msg_299 sequencing + msg_333 ratification with prior-art directive); Phase 1 closed-as-abandoned 2026-05-05; Phase 2 superseded by ORG-006 2026-05-05.
**HITL:** Ratified (original); superseded 2026-05-05 for Phase 2.
**Date:** 2026-05-02.
**Justifies via:** P1 (architecture over policy — eviction is structural; fix it structurally with a persistent process owning the bot connection, not with discipline around when to spawn Claude subprocesses), P3 (decisions justify upward — the ADR carries the upward chain from a recurring outage to a structural remedy), D1 (preserves the operator's bandwidth long-term — phone-side parity + eviction-proof messaging keeps the channel working without him needing to nurse it).

## Context

Pantheon's three Claude instances (Orchestrator/HO, Specialist/specialist, Recovery/recovery) each load the official Telegram channel plugin. The plugin uses long-poll `getUpdates` against the shared bot token. Telegram's Bot API enforces a single concurrent `getUpdates` per token: whichever subprocess polls last evicts the prior holder with HTTP 409. This is the dominant operational failure on the messaging plane.

Recovery's 2026-05-02 architectural research at `/tmp/architecture-handoff/transports-analysis-2026-05-02.md` confirmed the diagnosis is structural, not a config bug. The official plugin offers no daemon/webhook mode (Issues #1594 and #39808). The mid-session MCP disconnect during this very session was a fresh data point.

Three remediation options were surveyed:

- **Option A — `/remote-control`:** local-Claude bridge to claude.ai/code + mobile app. Phone-side slash-command parity. Doesn't fix eviction; orthogonal axis. Cheap probe.
- **Option B — Custom MCP transport bridge:** a persistent MCP server owns the bot connection; Claude instances attach as MCP clients via HTTP/SSE. Eviction-proof. ~1 day to build, or vet a community bridge (`tsgram-mcp`, `claude-code-telegram`).
- **Option C — Rich web UI ("kino"):** Tailscale-protected, multi-session view across Orchestrator/Specialist/Recovery, file browser, mobile + desktop. Realistic only as B + a frontend on top.

The operator ratified the sequencing **A → B → C** (Telegram msg_299) on 2026-05-02. A is a separate axis (orthogonal probe); this ADR ratifies the architectural commitment for **B and C** and treats them as two phases of one transport-bridge arc, since C is a frontend on top of B's substrate and they share architectural choices (multi-session model, Tailscale boundary, supervised single-process bot ownership).

This decision crosses the autonomy bar (D3): architectural, principle-touching, multi-week scope. ADR-tier ratification is required before implementation tickets are filed.

## Decision

Replace the in-process Telegram channel plugin with a **single persistent MCP server** (the "bridge") that owns the bot's poll loop and exposes inbound + outbound message tools to Claude instances over HTTP/SSE. Each Claude instance (Orchestrator, Specialist, Recovery) attaches as an MCP client. Subsequent rich-UI work consumes the bridge's session API.

Phased delivery; the phases share substrate but each is independently scoped:

### Phase 1 (B-arc) — Telegram MCP transport bridge

**Architecture:**
- One process, supervised by systemd, owns `getUpdates` for the bot token. No other process polls. Eviction is eliminated by construction.
- HTTP/SSE transport on loopback (or Tailscale) only. No public exposure.
- Tools exposed to Claude clients: `reply`, `react`, `edit_message`, `download_attachment`. API surface mirrors the existing plugin's tool shapes so worker prompts and CLAUDE.md guidance carry forward unchanged.
- Inbound dispatch: bridge maintains a routing table keyed by `chat_id` (and policy by `user`) → which Claude instance receives the inbound. v0 routing matches today's behavior (the operator's DM goes to Orchestrator; allowlisted DMs same; group messages dispatched per `telegram:access` skill rules). Routing config lives in the bridge, not in any Claude instance.
- Multi-session ready: the bridge can fan an inbound to one Claude instance at a time, or hold context for a "switchboard" view (Phase 2 consumes this). v0 keeps fan-out single-target; the API is multi-session-shaped.
- Auth between Claude clients and bridge: shared-secret token from `~/.claude/channels/telegram-bridge/.env`, scoped per client identity. Loopback by default; cross-host (Specialist) over Tailscale.
- Migration: bridge runs alongside the existing plugin during transition; per-instance cutover; plugin removed from `~/.claude/plugins/` once all three instances migrated. No big-bang.

**Build vs adopt:** open question; surfaced for the operator (see §Open questions). Default lean: prototype the build path in parallel with vetting community bridges, since the build is ~1 day and vendoring untrusted bot-side code carries supply-chain weight (cf. T-0021).

### Phase 2 (C-arc) — Web UI ("kino") on top of bridge

**Architecture:**
- Tailscale-protected web app. No public exposure.
- Multi-session view: simultaneous panes for Orchestrator, Specialist, Recovery, with per-session input + transcript + status. Phase 1's bridge already brokers the messages; Phase 2 visualizes them.
- Slash-command parity from phone (the `/remote-control` axis becomes redundant for slash-cmd parity once kino lands; see §Relationship to A).
- File browser scoped to each Claude instance's filesystem, read-only at v0.
- Mobile + desktop layouts. Stack TBD (open question — Next.js / SvelteKit / plain HTMX server-rendered all viable; pick at Phase 2 ratification).
- Deployment: same VPS, behind Tailscale. Bridge and web UI may co-locate in one process or split; design ticket decides.

Phase 2 implementation requires a **second ratification gate** at ticket-filing time. This ADR commits the architectural shape (web UI consumes the bridge's session API; multi-session-first; Tailscale-only) but does not authorize Phase 2 build. Phase 2 may be deferred indefinitely if Phase 1 + Option A together satisfy the operational gap.

**Prior art and reference architectures (researcher dispatch, 2026-05-02):**

A focused GitHub survey of web UIs for *local* Claude Code (not claude.ai/code) returned several active projects but no clean adopt candidate — all leading projects lack a documented private-network auth model, which is a hard requirement for kino's Tailscale-protected deployment. The strongest references for the build to crib from:

- **siteboon/claudecodeui** (10.5k stars, active April 2026) — multi-session view via auto-discovery from `~/.claude/projects/`, file browser, mobile-responsive React + Vite + Tailwind frontend, supports Claude Code + Cursor + Codex + Gemini CLI. **Reference for: session discovery + multi-session shape.** No documented auth model.
- **d-kimuson/claude-code-viewer** (1.2k stars, active April 2026) — JSONL-native TypeScript web client; reads `~/.claude/projects/`, presents session hierarchy, allows starting/resuming sessions and approving tool permissions. **Reference for: JSONL-native interaction model.** Same auth gap.
- **Adoom666/CloudeCode** (8 stars, active April 2026) — Electron menu-bar + FastAPI + tmux + xterm.js. **TOTP auth + JWT (15-min window + 7-day refresh), LAN-only threat model, Tailscale-compatible.** Mobile-optimized responsive UI with touch D-pad. Single-session limited. **Reference for: auth model and Tailscale-compatible deployment.**
- **hoangsonww/Claude-Code-Agent-Monitor** (197 stars, active) — Node + Express + React + SQLite + WebSockets; Claude Code hook events POST to API and broadcast to browser. D3 visualizations, Kanban view. **Reference for: hooks-driven event pipeline.**
- **mtzanidakis/praktor** (25 stars, active, May 1 2026) — Telegram → multi-agent Claude Code in Docker → "Mission Control" web dashboard. Per-agent isolation, SQLite memory, scheduled tasks, Whisper voice. Most ambitious architecture; web UI is secondary to orchestration. **Too opinionated to fork cleanly; reference for: multi-agent isolation patterns.**

**Adopt-vs-greenfield verdict:** greenfield with the four references above as reference implementations is likely cleaner than forking the largest candidate (claudecodeui) and retrofitting a security layer it wasn't designed for — unless the team is comfortable owning that fork surface long-term. Phase 2's design ticket re-evaluates this decision when actual build commitment is made; the ADR does not pre-decide it.

**Out-of-scope projects** (mentioned only for completeness): generic LLM web UIs (LibreChat, Open WebUI) do not target local Claude Code's session model; tsgram-mcp's web dashboard is an MCP-management surface, not a session UI; claude-code-telegram variants have no web UI.

### Relationship to Option A

Option A (`/remote-control`) is **orthogonal** — it does not fix eviction; it addresses phone-side TUI parity (slash commands, perm prompts, file viewing). Probing A is cheap and independent. This ADR neither requires nor blocks A. If A is satisfying, it covers part of Phase 2's value (slash-command parity from phone) and may reduce C-arc urgency. Phase 2's *unique* value over A is multi-session view across the fleet, which A cannot provide.

## Consequences

**Positive:**
- Eviction-proof messaging by structural enforcement (P1). Claude subprocess churn no longer affects the bot connection.
- Multi-session-shaped API from day one. Phase 2 doesn't require a redesign of Phase 1.
- Plugin tool shapes preserved → worker prompts and CLAUDE.md discipline (e.g., the smell test for the reply tool) carry forward unchanged.
- Single auditable process for inbound/outbound. Direct-curl fallback (already in use during this session's MCP outage) remains available for emergencies but is no longer the primary path.
- Substrate-portable: a future second channel (web inbound, Slack, etc.) plugs into the same bridge. Not built in v0; the abstraction lands when a second channel actually arrives (not before — P2).

**Negative:**
- The bridge becomes a single point of failure for the messaging plane. Mitigation: systemd `Restart=always`, supervised the same way `pantheon-watch.timer` is. Net resilience is still higher than the status quo (where eviction is the SPOF, just less visible).
- Phase 1 adds a new process to operate. Mitigation: bridge is small + single-purpose; lifecycle mirrors `tools/watch_runner.py` patterns the team already maintains.
- Phase 2 is ~1 week+ scoped and may stall. Mitigation: gated by separate ratification; Phase 1 stands on its own.
- Adopting a community bridge inherits supply-chain risk (cf. T-0021 MCP supply-chain audit). Mitigation: vet first; if adopted, pin commit + audit before merge.
- Telegram plugin's tool surface is the de-facto interface; if the official plugin gains daemon mode (Issue #1594) we will have built parallel infrastructure. Acceptable: the official daemon is hypothetical; eviction is current; we can deprecate the bridge in favor of the official daemon if and when it ships.

**Deferred / out of scope for this ADR:**
- Replacing the underlying MCP architecture itself (out of scope per analysis).
- Migration to Slack / Matrix / other channels (out of scope per analysis; the bridge's channel-agnostic API shape is design-time only, not delivered).
- Webhook mode against Telegram's Bot API (the bridge could add it later as an internal optimization; v0 long-poll is fine since only one process polls).
- Phase 2 stack pick (gated by Phase 2 ratification).
- Option A (`/remote-control`) probe: tracked separately in T-0024 (TBD on ticket-tree filing per `parent_ticket`).

**Open questions:**
- **Build vs adopt for Phase 1.** Default lean: prototype build in parallel with vetting `tsgram-mcp` and `claude-code-telegram`. Decision deferred to the Phase 1 design ticket. The operator's input welcome — supply-chain posture (T-0021) applies.
- **Routing config location.** v0: file in the bridge's working dir, version-controlled here or in a dotfile. Final shape decided at Phase 1 design.
- **Whether to colocate the bridge with `pantheon-watch` machinery on Specialist** vs run on Orchestrator's host. Default lean: Orchestrator's host (orchestrator) since Orchestrator is the primary Telegram consumer; Specialist and Recovery attach over loopback or Tailscale. Final shape at Phase 1 design.
- **Phase 2 stack pick.** Deferred to Phase 2 ratification.
- **Bridge as the substrate for `/remote-control`-style relay** (i.e., bridge brokers Anthropic's claude.ai/code <-> local sessions in addition to Telegram). Out of scope for v0; revisit only if both A is unsatisfying and we want a unified inbound surface.

**Relates to tensions:**
- Architecture-time generalization vs YAGNI (P2 / P6): the API is multi-channel-shaped at design time but only Telegram is delivered. The shape is cheap; the second-channel build is not. Tension preserved by deferring the second-channel work until evidence of need.

## Implementation outline (post-ratification)

The implementation arc files as ticket tree per ORG-003 conventions; this section sketches the shape and is non-binding until tickets are filed.

**Phase 1 ticket tree (B-arc):**
1. **Design ticket:** API spec (tool shapes, transport, auth, routing config format, migration plan). Plan-first dispatch to implementer; reviewer pre-flight.
2. **Build ticket:** bridge process (Python FastAPI + MCP SDK leans default; revisit at design). Tool implementations + poll loop + routing.
3. **Systemd unit ticket:** unit + restart policy + log rotation. Mirrors existing pantheon-watch units.
4. **Migration ticket:** parallel-run guidance, cutover script, plugin removal, verification on each instance (Orchestrator, Specialist, Recovery).
5. **Decommission ticket:** plugin removed from `~/.claude/plugins/` across all three users; documentation updated; this ADR's status moves to "Implemented."

**Phase 2 ticket tree (C-arc) — gated by separate ratification:**
1. Design ticket: web UI surface, stack pick, multi-session model, file browser scope.
2. Build ticket: frontend implementation against bridge's session API.
3. Tailscale + deployment ticket.
4. Mobile + desktop layout polish.

Both arcs respect the standing operational disciplines: HERMES guard (no contamination in commits or code), narrow-scope auto-commits, plan-first implementer dispatch + reviewer pre-flight, DECISIONS sections in worker reports, no force-push.

## Related

- [transports-analysis-2026-05-02](/tmp/architecture-handoff/transports-analysis-2026-05-02.md) — Recovery's research and the operator's locked pick.
- [ORG-001](ORG-001-ledger-format.md) — append-only audit chain; ledger entries reference this ADR by ID.
- [ORG-003](ORG-003-ticket-system.md) — ticket lifecycle the implementation arcs use.
- `knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md` — context on the eviction classifier and the discipline scope.
- BACKLOG B6 — Pantheon ↔ Echo handoff connection. Indirectly related (a working bridge improves cross-project messaging) but not gated by this ADR.
- T-0021 — MCP supply-chain audit; informs the build-vs-adopt question for Phase 1.
