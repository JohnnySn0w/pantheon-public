---
aliases: [ORG-006]
adr-id: ORG-006
title: kino PWA — Pantheon multi-agent web UI (post-bridge pivot)
status: Ratified
date: 2026-05-05
author: orchestrator (Orchestrator)
hitl: Ratified
ratified_by: operator
ratified_via: telegram (msg_660 design ack + msg_662 Q1-Q5 answers + Specialist-topology clarification)
related:
  - knowledge/episodic/2026-05-05-pwa-architecture-survey/notes.md
  - knowledge/episodic/2026-05-06-kino-cross-uid-architecture/notes.md
  - tickets/active/T-0074.md
  - tickets/done/T-0091.md
  - tickets/done/T-0073.md
  - tickets/done/T-0099.md
  - tickets/done/T-0100.md
  - tickets/done/T-0104.md
  - adr/ORG-004-mcp-transport-bridge.md
  - rulebook/principles.md (P1, P3, P5)
  - rulebook/directives.md (D1, D3)
addenda:
  - 2026-05-06 — Phase 1 implementation addendum (post-deploy as-built shape; Deviation 1 systemd subservices + kino-launcher.sh pre-spawn; Deviation 2 socat ACCEPT-FD:3 stdio↔socket bridge); HO-authored under autonomy-bar D3, ratified inline via DECISIONS-with-justifies-via on pantheon-pwa commits 0c0f071 + d42992d + b920068; ledger close_kino_phase_1b_cross_uid_architecture.
  - 2026-05-21 — Agent-chat sunset amendment (T-0508); headless claude -p agent backend removed because claude -p begins billing paid API 2026-06-13 (breaks the flat-sub cost model); Telegram remains the chat surface; PWA retains ticket-UI/stats; chat frontend kept inert for future revival. operator-decided msg_1834/1836; HO-authored. See §Amendment 2026-05-21.
build_disposition: built
---

# ORG-006 — kino PWA: Pantheon multi-agent web UI

**Status:** Ratified 2026-05-05 by the operator (Telegram, msg_662 Q1-Q5 answers + Specialist-topology clarification on top of msg_660 design ack). **AMENDED 2026-05-21 (T-0508): the agent-chat backend is sunset — see §Amendment 2026-05-21 at the end. The ticket-UI/stats PWA remains live; the multi-agent-chat design is dormant (preserved in history, not deleted).**
**HITL:** Ratified.
**Date:** 2026-05-05.
**Justifies via:** P1 (architecture over policy — per-agent identity collapse must be impossible by construction, not enforced by discipline), P3 (decisions justify upward — the bridge's failure is the load-bearing why), P5 (separation of orchestration and execution — kino is the comms substrate; agent execution lives in the per-agent stdio subprocesses), D1 (the operator's bandwidth — the comms plane must work without him nursing it), D3 (autonomy bar — ADR-tier ratification gate is explicit, build dispatches gate on ratification).

## Context

The Telegram-bridge transport (ORG-004 Phase 1) was ratified 2026-05-02, built and deployed across the fleet on 2026-05-05 (T-0073 fan-out: T-0078, T-0087, T-0088 + Orchestrator cutover), and declared a nonstarter the same day (T-0091, the operator directive 2026-05-05). The structural failure: a single `BOT_TOKEN` held by the bridge daemon, plus a `chat_id → agent` routing table, meant **all agent outbound consolidated onto one bot identity** (Orchestrator's), and the operator's DMs to Recovery were being answered from Orchestrator's bot identity. Splitting outbound by agent would require substantive bridge daemon redesign (multi-poller, or message-prefix disambiguation).

The hard structural lesson: **per-agent identity collapse must be impossible by construction**. Not "preventable with discipline" — *impossible*. Any future architecture that introduces a single broker token, single shared connection, or single routing chokepoint re-introduces the failure surface.

ORG-004 Phase 2 ("kino" web UI) was originally framed as a frontend on top of the Phase 1 bridge substrate. With Phase 1 shelved, kino becomes the comms substrate itself, not a frontend on top of one. This ADR commits the architectural shape for that revised scope. T-0091 elevated kino (T-0074) to priority #1; the survey at `knowledge/episodic/2026-05-05-pwa-architecture-survey/notes.md` (5531 words, 7 sections + DECISIONS) provides the input. This ADR is the synthesis.

ORG-004's Phase 1 sections are now historical (closed-as-abandoned via T-0073). ORG-004's Phase 2 architectural commitments are superseded by this ADR — the bridge-substrate assumption no longer holds. ORG-004 is not retracted (Phase 1 was a real attempt that produced load-bearing structural lessons); it's marked superseded-for-Phase-2 in §Related.

## Decision

Build **kino**: a Tailscale-protected, single-user, multi-agent Progressive Web App that lets the operator interact with Pantheon's three Claude Code instances (Orchestrator, Specialist, Recovery) through one frontend. Each agent maintains distinct addressable identity at the OS level. Greenfield build; no fork.

### Per-agent identity model

**Per-agent stdio MCP subprocess, PWA-routed messages.** At kino backend startup, the backend spawns three subprocesses — one per agent — each running the agent's `claude` binary in stdio-MCP mode under a distinct OS-level identity (separate unix user where applicable, separate working directory, separate `~/.claude/projects/` namespace). Identity is baked at the process level: distinct PIDs, distinct stdin/stdout pipes. The PWA frontend routes messages to a named agent slot (`/api/agents/orchestrator/send`); the backend writes to the corresponding subprocess stdin and reads from its stdout. Every WebSocket frame and every HTTP response carries a mandatory `agent_id` field; the backend refuses to emit frames without it; the frontend drops untagged frames with a console error rather than rendering them in a default pane.

**All Pantheon Claude accounts live on the same VPS** as distinct unix users; today: `claude` (Orchestrator), `claude-research` (Specialist), `claude-fallback` (Recovery). The set is **not fixed** — kino must accommodate an arbitrary number of agents over time as new instances are spun up (the operator clarification msg_664). Per-agent identity at the OS level means the kino backend spawns each registered agent's `claude` subprocess under its respective unix UID. Subprocess invocation pattern: `sudo -u <agent_unix_user> claude --mcp-server` (or equivalent setuid handling). Architecture:

- **Agent registry** is the source of truth. Stored at `/etc/kino/agents.toml` (or equivalent), readable by the kino service-user, writable only by root. Each entry: `name`, `unix_user`, `working_dir`, optional `display_name`. The kino-daemon reads the registry at startup and on `SIGHUP`/`systemctl reload` to handle additions/removals without full restart. The SQLite `agents` table mirrors registry state at startup; foreign keys preserve historical message attribution even if a registry entry is later removed.
- **Sudoers scope is pre-allocated, not wildcarded.** The kino service-user gets a sudoers rule listing N specific unix users it may spawn-as, restricted to the `claude` binary path with no other privilege. v0 default: N=8 (existing 3 + 5 future-reserved: `claude-future-{1..5}`). When the operator spins up a new instance, he chooses one of the reserved slots; when N is exhausted, a sudo-block ticket adds more slots. Wildcard sudoers (`claude-*`) is rejected as a hardening anti-pattern. A future iteration can regenerate the sudoers fragment from the registry to make this fully dynamic, but that's not v0.
- **Frontend renders N panes**, not a hardcoded three. The PWA fetches `/api/agents` at load and renders one pane per registered agent; layout adapts (tabbed view on mobile, multi-pane on desktop) as N grows.

This is structurally tighter than running all subprocesses as the same unix user — even a kino-side subprocess-routing bug cannot cause one agent to *be* another at the OS level. Survey §1 Option B (remote-host session manager) is documented for completeness but is not adopted at v0 since the same-VPS topology removes the cross-host transport need.

Why not a single backend session manager that proxies all three agents? Software-discipline enforcement of agent identity (always tag the right `agent_id`) is the bridge's failure mode at the application layer. OS-level enforcement (distinct PIDs cannot share response buffers by accident) is structural. P1 favors the structural fix.

### Auth model

**Tailscale Serve + identity headers as v0 auth.** The kino backend binds `127.0.0.1` only and is fronted by `tailscale serve`, which terminates TLS via Tailscale's cert machinery and is reachable only from devices on the tailnet. Tailscale Serve injects three identity headers into proxied requests (`Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`); the backend reads `Tailscale-User-Login` to verify the request comes from the operator. Headers are stripped and re-injected by Tailscale Serve and cannot be spoofed as long as the backend is localhost-only. **Security invariant: the backend must never bind 0.0.0.0.**

For WebSocket connections, the HTTP Upgrade request carries the identity headers; the backend issues a short-lived signed session cookie on Upgrade for subsequent WS frames.

CloudeCode's TOTP + JWT pattern (15-minute access token, 7-day refresh with reuse detection, JWT in `Sec-WebSocket-Protocol` header) is documented as a defense-in-depth upgrade path if the threat model expands beyond a single-user trusted tailnet. Not adopted at v0 — adds key management overhead before kino exists.

### Stack

**SvelteKit** for the frontend; **Node.js** (or Bun) for the backend; **SQLite** (WAL mode) for state.

- SvelteKit ships ~15-25 KB JS baseline (vs Next.js ~80-90 KB) — relevant for iOS PWA install + mobile bandwidth. First-class PWA tooling via `vite-plugin-pwa`. Solo-dev velocity is good. Vercel-backed (Rich Harris), 5-year track record.
- HTMX is the credible alternative if the operator prefers zero-JS-build. Streaming UX (agent responses streaming back) is more awkward via HTMX SSE than via SvelteKit WebSocket. **Open question:** SvelteKit vs HTMX — recorded as Q1 below for the operator's call.
- Backend stays in the Node ecosystem to keep one runtime. Bun acceptable if the operator prefers (already adopted elsewhere — pantheon-bridge runs Bun).
- SQLite is the default per P1 (minimize dep surface). No at-rest encryption v0 — Tailscale handles network threat; SQLCipher key-management on a VPS provides marginal protection given the key must be env-var-readable by the running process. Filesystem-permission hardening (`chmod 600` on the DB, dedicated unix user for kino service) provides meaningful protection at zero cost.

### Web Push (iOS-aware)

**VAPID keys via the `web-push` npm package; service worker per the survey's pattern; iOS 16.4+ minimum; in-PWA polling fallback for non-installed devices.** iOS gotchas (home-screen install gate, no `beforeinstallprompt`, permission-inside-user-gesture, 7-day cache expiry) are surfaced in the implementation tickets. Background frequency caps for the service worker `push` handler are tagged `[weak]` in the survey (Apple does not publish numbers); iOS-side behavior gets an empirical soak in Phase 3.

Push notifications are additive: WebSocket on the active pane is the real-time channel; Push delivers when the PWA is backgrounded. Stale subscription cleanup on 410 Gone responses from the push endpoint is mandatory.

### Session state schema

Five SQLite tables (sketched in the survey §5): `agents`, `sessions`, `messages`, `push_subscriptions`, `agent_state`. Indexes on `(session_id, ts)` and `(agent_id, ts)`. Numbered migration files (`migrations/NNN_name.sql`); no ORM. Single-writer pattern: only the main kino backend writes to SQLite; agent subprocesses communicate results via IPC (pipe or local HTTP), never via direct DB writes.

### Greenfield build, no fork

The survey re-evaluated five prior-art projects (siteboon/claudecodeui, d-kimuson/claude-code-viewer, Adoom666/CloudeCode, hoangsonww/Claude-Code-Agent-Monitor, mtzanidakis/praktor) under the per-agent-identity lens. None solve the named-agent-routing problem cleanly enough to fork:

- claudecodeui: session-as-project model doesn't map to named agents.
- claude-code-viewer: JSONL-native, but same session-not-agent model.
- CloudeCode: hard single-session invariant; multi-agent is architecturally out of scope.
- Agent-Monitor: monitoring only; no message-in path.
- praktor: Telegram-primary I/O (which we just shelved); Docker complexity Pantheon hasn't adopted; web UI is secondary.

References lift cleanly: praktor's per-agent isolation model + AGENT.md per-agent identity convention; CloudeCode's auth pattern as upgrade path; Agent-Monitor's SQLite schema shape; claudecodeui's mobile-responsive multi-pane UX. Greenfield with targeted pattern-lifting.

## Implementation phases

Each phase gates on the prior phase passing supervised first-fire with the operator. No big-bang.

### Phase 1 — Backend skeleton

- `kino` daemon (Node/Bun) running as its own systemd unit + dedicated unix user (clean threat model from day one — no claude-account compromise → kino takeover).
- Agent registry at `/etc/kino/agents.toml` (root-writable, service-user readable); kino-daemon reads at startup and on `SIGHUP`. Initial registry contains the existing 3 agents (Orchestrator/Specialist/Recovery) plus the structure for future additions.
- Per-agent subprocess spawner: spawn one `claude` stdio-MCP subprocess per registered agent under its respective unix UID via `sudo -u`. Kino-daemon's service-user gets a sudoers rule with N=8 pre-allocated slots (`claude`, `claude-research`, `claude-fallback`, `claude-future-1` through `claude-future-5`) running specifically the `claude` binary path — no broader privilege. Add/remove from the registry triggers spawn/kill of the corresponding subprocess on `SIGHUP`.
- SQLite schema migrations applied at startup; single-writer enforcement.
- Tailscale Serve fronting localhost; identity-header validation middleware; localhost-only bind invariant tested.
- HTTP API: `GET /api/agents`, `POST /api/agents/:id/send`, `GET /api/agents/:id/messages` with pagination.
- WebSocket: `/ws/agents/:id` per-agent, agent_id-tagged frames mandatory; schema-enforce on emit.
- No frontend yet. Tested via `curl` + `wscat` against Tailscale.
- 5-risk countermeasures (survey §7) baked in at code-review checklist.

### Phase 2 — PWA frontend

- SvelteKit shell with manifest.webmanifest + service worker + iOS-aware install prompt.
- N-pane multi-agent view (fetches `/api/agents` at load; renders one pane per registered agent; tabbed-on-mobile / multi-pane-on-desktop layout adapts to N); per-agent message thread; agent_id rendering invariant tested.
- WebSocket reconnect logic; offline indicator; retry on transient 5xx.
- Slash-command passthrough — typed `/foo` in the input is forwarded verbatim to the agent's stdio session, which dispatches via the agent's existing skill/slash-command registry. Kino does not maintain a command catalog beyond a small set of session-control meta-commands (`/clear`, `/restart`).
- No Push yet (use pure WS for real-time).

### Phase 3 — Web Push

- VAPID keys generated; service worker subscribes; backend stores `push_subscriptions`.
- iOS install-prompt UX hardened; Push only enabled after detected home-screen install.
- Stale subscription cleanup on 410 Gone.
- Soak: 1 week of mixed-mode use (PWA backgrounded vs foreground) before treating Push as production.

### Phase 4 — Cutover + bridge teardown

- Per-agent comms migrate from Telegram + bridge → kino. Telegram remains as fallback only until soak completes.
- Bridge teardown via T-0093 (already filed, depends_on: [T-0074]): sudo systemd disable + stop pantheon-bridge.service, /opt cleanup, /etc cleanup, BOT_TOKEN rotation, bridge-tree blocked tickets sweep-as-moot, pantheon-bridge repo archival.
- Orchestrator's MCP cleanup: `claude mcp remove -s user telegram-bridge-stdio`, settings revert.
- T-0089 (Cofounder2 memory eval) re-evaluation in T-0093 scope.

## Phase 1 implementation addendum (2026-05-06, post-deploy)

Phase 1a + Phase 1b are both shipped and supervised-first-fire-green as of 2026-05-06. The §Decision and §Implementation-phases sections above record the design-time intent. Two structural deviations surfaced at deploy time and are documented here as the as-built shape; both were ratified as autonomy-bar D3 calls with DECISIONS-with-justifies-via on the relevant pantheon-pwa commits, captured in `tickets/done/T-0100.md` + `tickets/done/T-0104.md` transition logs and `ledger/2026.jsonl` action `close_kino_phase_1b_cross_uid_architecture` (2026-05-06T21:55Z).

### Deviation 1 — kino-daemon does NOT issue cross-UID spawns at runtime; per-agent systemd subservices + kino-launcher.sh pre-spawn

The §Decision §Per-agent-identity-model spec was that kino-daemon would spawn each agent's `claude` subprocess via `sudo -u <agent_unix_user>`. Verified at deploy: Bun 1.3.13's `posix_spawn` returns `EACCES` when called from kino-as-systemd-service AFTER the daemon's full module-import graph has loaded (Tests A-J bisect chain — single inline ops work; daemon-import context fails; cause not identified after exhaustive triage of AppArmor, NoNewPrivileges, sudoers, mount namespaces, and Bun's documented `uid`/`gid` options which silently no-op).

As-built shape:

- **Per-agent systemd subservices.** Each registered agent gets its own systemd template instance: `kino-agent@<user>.service` + `kino-agent@<user>.socket`. The .service runs as `User=<user>`; the .socket pre-creates `/run/kino/agents/<user>.sock` as `kino:kino` mode 0600 (so kino-daemon-as-kino is the only thing that can connect). Source-of-truth examples at `pantheon-pwa/scripts/systemd/kino-agent@.service.example` + `kino-agent@.socket.example`.
- **`kino-launcher.sh` pre-spawn.** `kino.service`'s `ExecStart=/usr/local/bin/kino-launcher.sh`. The launcher is bash, runs `systemctl start kino-agent@<user>.service` for each registered agent, then `exec`'s the bun daemon. Bash spawns systemctl reliably (no Bun spawn restriction); `exec` replaces the process so kino-daemon inherits the parent slot. Kino-daemon's runtime cross-UID spawn becomes a NO-OP.
- **Polkit rules grant the kino user permission to manage `kino-agent@*.service` instances** via dbus (start/stop/restart/reload). Source-of-truth example at `pantheon-pwa/scripts/polkit/10-kino-agents.rules`.
- **Sudoers fragment retired.** The N=8 pre-allocated sudoers slots described in §Per-agent-identity-model are NOT installed in production. Privilege scope moves entirely to the polkit rule (kino can manage `kino-agent@*` units) + per-agent systemd subservice (each unit's `User=<user>` is the privilege drop).
- **Same-UID spawn-as-self (T-0099) retained as fallback in `src/agents/spawner.ts`** — if `entry.unix_user === userInfo().username` at spawn time, branch to direct `Bun.spawn(claude_bin, ...)` without going through any cross-UID path. This was Phase 1a's structural fix; left in place as a same-UID optimization.

Per-agent identity at the OS level is preserved — actually strengthened, since each agent now runs in its own systemd unit with `User=<unix_user>` enforced by systemd at exec time, a tighter boundary than `sudo -u` would have provided.

### Deviation 2 — IPC uses socat ACCEPT-FD:3 EXEC: as the stdio↔socket bridge

The Phase 1b research (`knowledge/episodic/2026-05-06-kino-cross-uid-architecture/notes.md`) proposed `StandardInput=socket` + `StandardOutput=socket` directives in `kino-agent@.service` to wire kino-daemon ↔ claude IPC via systemd socket activation. Verified at deploy: all three kino-agent units crashed in `start-limit-hit` because `claude mcp serve` speaks pipe-stdio — `StandardInput=socket` passes the *listening* socket fd as stdin (the unaccepted listener), which is structurally not what claude expects.

As-built shape:

- `ExecStart=/usr/bin/socat ACCEPT-FD:3 EXEC:"/home/%i/.npm-global/bin/claude mcp serve"` — socat 1.8+'s `ACCEPT-FD:3` consumes the systemd-passed listening fd (LISTEN_FDS=1 env, fd=3), accepts the kino-daemon connection, and bridges the connected fd to claude's stdin/stdout pipes via the EXEC subcommand.
- `StandardInput=socket` / `StandardOutput=socket` directives REMOVED from the unit.
- Socket activation retained — the `Sockets=kino-agent@%i.socket` directive still passes the systemd-managed listening fd (security boundary preserved: kino:kino mode 0600 ownership).

This pattern generalizes: anytime systemd socket activation needs to feed a stdio-pipe binary, `socat ACCEPT-FD:N EXEC:"..."` is the structural answer rather than `StandardInput=socket`. Memory-pinned at `feedback_systemd_socket_activation_socat_bridge.md`.

### Invariants preserved

The deviations are surface — the load-bearing architectural commitments hold:

- **Per-agent identity at the OS level** — each agent's claude process runs with `User=<unix_user>`; distinct PIDs cannot share response buffers by construction.
- **Mandatory `agent_id` tagging** on every API response and WebSocket frame — enforced in code (D5/D6 tests on the pantheon-pwa side).
- **Localhost-only bind + Tailscale Serve front door** — `BindInvariantError` thrown if `127.0.0.1` is not the bind address.
- **Single-writer SQLite** — kino-daemon is the only DB writer; agent IPC results route to the daemon, never to direct DB writes.

### What this means for downstream phases

Phase 2/3/4 plans and acceptance criteria are unaffected. Frontend speaks to kino-daemon's HTTP+WS API (deviations are below the API). Phase 4 cutover still owns sudoers cleanup but the scope shrinks: only the `pantheon-bridge` sudoers fragment needs removal — kino was never installed with one. T-0093 cleanup ticket transition log to be amended at Phase 4 close.

## Ratification answers (the operator, 2026-05-05 msg_662)

**Q1 — Frontend stack: SvelteKit.** Confirmed.

**Q2 — Specialist topology — clarified by the operator.** Specialist is NOT on a separate VPS; all three Pantheon accounts (claude=Orchestrator, claude-research=Specialist, claude-fallback=Recovery) live on the same VPS as distinct unix users. The per-agent stdio subprocess pattern applies uniformly to all three; no Option-B cross-host complication. The original Q2 framing was based on an HO assumption error — corrected in §Decision above. (HO memory pinned at `project_pantheon_account_topology.md` to prevent recurrence.)

**Q3 — Slash-command scope: passthrough.** Spec'd now per the operator: kino does not define a custom command catalog. Slash-commands typed in the PWA pass through to the agent's existing Claude Code session — whatever skills + slash-commands the agent's CC session exposes are what the user can invoke. PWA is a thin transport for the agent's existing command surface. Kino-meta commands are limited to session-control (`/clear`, `/restart`, etc.) — no agent-domain commands defined kino-side.

**Q4 — Implementation owner: Team + project-orchestrator.** Per the b9-validated pattern. HO pre-stages implementer as teammate + reviewer is HO-on-demand one-shot (per memory `feedback_team_orchestrator_tool_restriction.md`).

**Q5 — Dispatch trigger: file + dispatch immediately on ratification.** Confirmed.

## Justifies via (expanded)

- **P1 (architecture over policy):** The bridge failed because identity collapse was a software-level invariant enforced by routing-table discipline. kino enforces identity at the OS level (distinct PIDs cannot share response buffers). Identity collapse is impossible by construction, not preventable by code review.
- **P3 (decisions justify upward):** This ADR carries the chain — bridge structural failure (T-0091) → per-agent identity as load-bearing requirement → OS-level subprocess enforcement → kino architectural shape. Every downstream implementer ticket references this ADR as the why.
- **P5 (separation of orchestration and execution):** kino is the comms substrate (orchestration plane); agent execution lives in per-agent stdio subprocesses (execution plane). The PWA frontend doesn't speak to agents — it speaks to kino, which routes.
- **D1 (the operator's bandwidth):** The current state (Telegram + bridge declared nonstarter, mixed comms paths) burns the operator's attention on figuring out which bot identity is which agent. kino collapses that to one UI with named agents, restoring the comms plane to the "works without nursing" state D1 requires.
- **D3 (autonomy bar):** This ADR sits above the bar. The operator ratifies the architectural shape; HO autonomously dispatches Phase 1 implementation post-ratification with the standard chain-of-justification discipline. Phase 2/3/4 each get supervised first-fire gates per the multi-phase pattern.

## Related

- **`knowledge/episodic/2026-05-05-pwa-architecture-survey/notes.md`** — survey input; this ADR synthesizes it.
- **`knowledge/episodic/2026-05-06-kino-cross-uid-architecture/notes.md`** — Phase 1b cross-UID design input; the as-built shape diverges (see §Phase-1-implementation-addendum).
- **`tickets/active/T-0074.md`** — design ticket; the 9 design questions (Q1-Q9, including post-pivot Q9 per-agent identity) map to this ADR's decisions.
- **`tickets/done/T-0091.md`** — strategic-pivot directive; this ADR is the design output of T-0091 fan-out task (d).
- **`tickets/done/T-0073.md`** — abandoned bridge cutover; the structural blocker rationale is the why.
- **`tickets/done/T-0099.md`** — Phase 1a same-UID spawn-as-self pattern (retained in §addendum).
- **`tickets/done/T-0100.md`** — Phase 1b cross-UID architecture parent; addendum's Deviation 1 + 2 close-out trail.
- **`tickets/done/T-0104.md`** — Phase 1b Cycle B IPC plumbing; socat ACCEPT-FD bridge close-out trail.
- **`adr/ORG-004-mcp-transport-bridge.md`** — Phase 1 historical (closed via T-0073); Phase 2 superseded by this ADR. ORG-004 is not retracted — its structural lessons are load-bearing for this design.
- **`tickets/blocked/T-0093.md`** — post-PWA cleanup ticket; depends_on: [T-0074]; absorbs bridge teardown + bridge-tree blocked tickets sweep-as-moot.
- **Memory pins (claude auto-memory):** `feedback_bun_spawn_systemd_service_eacces.md` (pre-spawn-via-launcher pattern), `feedback_systemd_socket_activation_socat_bridge.md` (socat ACCEPT-FD:3 bridge).
- **`rulebook/principles.md`** P1, P3, P5; **`rulebook/directives.md`** D1, D3.

## Ratification

Ratified 2026-05-05 by the operator (Telegram msg_662). Post-ratification execution:

1. ORG-004 marked superseded-for-Phase-2 (front-matter status update + footnote in §Decision).
2. Phase 1 implementation ticket-tree filed (T-0095: kino backend skeleton — multi-deliverable parent for project-orchestrator decomposition into kino-daemon scaffold, per-agent subprocess spawner, SQLite migrations, Tailscale bind validation, HTTP/WS API surface).
3. Team + project-orchestrator dispatched per Q4 ratified answer (b9-validated pattern; implementer pre-staged as teammate; reviewer HO-on-demand).
4. T-0074 transition log entry: "ratified ORG-006; Phase 1 implementation ticket-tree filed (T-0095)."
5. Ledger entry justifies-via {P1, P3, P4, P5, D1, D2, D3}.

**Phase 1 implementation addendum (2026-05-06):** Authored by HO post-deploy under autonomy-bar D3. Captures the two as-built deviations (per-agent systemd subservices + kino-launcher.sh pre-spawn pattern; socat ACCEPT-FD:3 stdio↔socket bridge) discovered during Phase 1b deploy. Each deviation's DECISIONS-with-justifies-via lives on the pantheon-pwa commits (0c0f071 + d42992d + b920068) and the ticket transition logs (T-0100, T-0104). Ledger entry: `close_kino_phase_1b_cross_uid_architecture` (2026-05-06T21:55Z). The architectural commitments above (per-agent identity at OS level, mandatory `agent_id` tagging, localhost-only bind, single-writer SQLite) are unchanged.

## Amendment 2026-05-21 — Agent-chat backend sunset (T-0508)

**Decision (the operator, Telegram msg_1834 + msg_1836, 2026-05-21):** the kino PWA no longer uses headless `claude -p` sessions for its chat interface. The agent-chat backend — the multi-agent-conversation premise at the heart of this ADR — is **sunset**.

**Why.** Specialist surfaced, and the operator confirmed, that headless `claude -p` invocations begin consuming **paid API usage as of 2026-06-13**. Pantheon's operating cost is flattened onto the monthly Claude subscription (heavily subsidized); API usage breaks that subsidy. Every kino-agent instance was a headless `claude -p` — a post-June-13 cost liability with no offsetting value now that Telegram is the stable chat surface for all three personas. P1/D1: removing the cost-bearing affordance structurally beats remembering not to use it.

**What was executed this session (T-0508; pantheon-pwa main `e394235` + `3a0f393`):**
- Code: removed the agent subsystem (`src/agents/*`), the per-agent spawn loop in `kino-launcher.sh`, `kino-drop-in-writer.sh`, the agent IPC/routing + `/api/agents/*` + chat-only push routes, and the agent WebSocket handler. Orphaned `repository.ts` agent functions removed; `agents` table schema retained (no migration-drop).
- Infra: tore down all `kino-agent@*.{service,socket}` units + per-instance drop-ins + the `@`-templates, removed the `kino.service` `ExecStartPre` drop-in-writer hook, deleted the `10-kino-agents.rules` polkit grant and the dead Section-A (`kino → run claude binary`) sudoers grant (`visudo`-validated; live Section-B stats grant kept). The 3 running headless `claude -p` agents were stopped.
- Process: plan-first implementer + reviewer (APPROVE) + HO-orchestrated deploy/teardown.

**Dormant vs. retained.** The multi-agent-**chat** design (per-agent subprocess spawn, the socat ACCEPT-FD:3 stdio↔socket bridge from the 2026-05-06 addendum, agent registry, per-agent OS identity) is **DORMANT** — removed from the running system but preserved in git history + the inert chat frontend (`web/src/routes/chat/`, nav-link removed) for a possible future revival if the cost calculus changes. **Retained live:** `kino.service` as the PWA backend serving the **ticket-UI + stats** surfaces (which read pantheon-config / ledger, not agent sessions), localhost-only bind, single-writer SQLite. Telegram remains the chat surface (ORG-004 lineage).

**Ratification.** operator-decided via Telegram (msg_1834 rationale + msg_1836 "execute now"); HO-authored record per ORG-012 (HO drafts ADR amendments). Execution chain: T-0508 transition log + the kino-chat-sunset ledger entry + pantheon-pwa commits above. Backing memory pin: `project_kino_chat_sunset_headless_api_billing`.

## Amendment 2026-05-27 — Auth-model carve-out: tag-owned client devices produce no identity header (T-0638)

**Clarification, not a change.** The original §Auth model section above asserts: "Tailscale Serve injects three identity headers into proxied requests (`Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`); the backend reads `Tailscale-User-Login` to verify the request comes from the operator." This is true for **user-owned source peers** but NOT unconditional — confirmed empirically 2026-05-27 (the operator msg_1987, msg_1990).

**Carve-out:** When a source device is owned by a **tag** (e.g. `tagged-devices`) rather than a user account, Tailscale serve still forwards the HTTPS request to the backend BUT omits the `Tailscale-User-Login` header entirely (because there's no user identity to propagate — the tag-ownership has no `<user>@<domain>` to serialize). Kino's `extractTailscaleIdentity` returns `null` and the request hits the `401 no_tailscale_identity` path. The distinction matters: this surfaces as "no header at all," NOT `user_mismatch` ("header present but rejected").

**Fix:** Reauth the client device in the Tailscale client to transfer ownership from the tag to the user account. NOT "just remove the tag" — a re-authentication flow on the client side is required (per msg_1990). After reauth, the device appears under the user in `tailscale status` and identity injection resumes.

**Why this matters for ORG-006.** The §Auth model section's load-bearing claim ("Tailscale Serve injects three identity headers ...") is now annotated as **user-ownership-conditional**, not unconditional. The security invariant ("backend must never bind 0.0.0.0") is unchanged — that defends against bypass attempts; this carve-out is about a legitimate use-case (the operator on a tag-owned device) producing an unhelpful 401 until the user reauths. Operationally documented at `sop/standard/operating-tailscale-services.md` §"Tag-owned client device → no Tailscale-User-Login" with full diagnosis order.

**Ratification.** Surfaced via T-0638; ratifiable_files names this ADR.
