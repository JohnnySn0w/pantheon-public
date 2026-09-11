---
aliases: [ORG-009]
title: kino PWA ticket-API contract
status: Ratified 2026-05-10
date: 2026-05-10
# re_evaluate: machine-checkable mirror of this ADR's inline re-eval prose,
# consumed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk.
# type: calendar|metric|event ; fires: auto-ticket (deterministic only) | surface.
# Backfilled per T-2530 (inline-only trigger was previously uncaught).
re_evaluate:
  - id: R1
    type: event
    when: "sustained write-rate becomes load-bearing"
    fires: surface
    text: "revisit the ticket-API write-rate handling if/when sustained write-rate becomes load-bearing"
build_disposition: built
---
# ORG-009 — kino PWA ticket-API contract

**Status:** Ratified 2026-05-10 (the operator Telegram msg_1166: "Looks good." with one wording fix on the Tailscale identity reference).
**HITL:** Ratified
**Date:** 2026-05-10
**Author:** Orchestrator (Orchestrator)
**Origin:** T-0144 sub-decision (a) ratified by the operator 2026-05-09 msg_838 ("yes — short ORG-NNN ADR for the ticket-API contract"); filed as T-0169 in inbox 2026-05-10 02:00 UTC.

## Context

T-0144 (kino PWA Phase 2.x: ticket interface + emoji support) surfaced demand for a ticket-management surface in the PWA — read tickets, file new, claim, resolve — without dropping back to terminal `tools/ticket.py`. The b33-ticket-state-sweep cycle alone consumed ~20 Telegram round-trips for what could have been ticket-status-flips + comments in a PWA pane; the bandwidth-cost is real and motivates Phase 2.x finish-priority per the operator 2026-05-09 msg_1151 ("this incident pushes us to finish the pwa as prio").

The ticket interface is the first PWA surface that wraps a pantheon-config artifact class with a structured API. Future PWA panes will follow the same shape: HANDOFF view, ledger view, ADR view, lessons view, incidents view, principles+directives read-only view. Each artifact class has the same load-bearing pattern:

- Directory of markdown files with YAML front-matter
- Optional lifecycle CLI (tickets have `tools/ticket.py`; ledger has `tools/spine_index_groom.py`-grooming-only; some have nothing)
- Append-only log artifact OR structured directory tree

Pinning a single API contract once prevents per-pane drift + makes future-pane authoring mechanical (paste the contract; map the resource).

## Decision

### API surface (HTTP + WebSocket)

**HTTP endpoints** (kino-daemon serves; Tailscale identity header gates access per ORG-006 §Auth):

```
GET    /api/tickets                      # list with filters (lane / target / label / requester / age)
GET    /api/tickets/:id                  # full frontmatter + body sections
POST   /api/tickets                      # file new (body: full ticket payload)
POST   /api/tickets/:id/claim            # claim subcommand passthrough
POST   /api/tickets/:id/block            # block-with-reason (body: {"reason": "..."})
POST   /api/tickets/:id/unblock          # unblock-with-reason (body: {"reason": "..."})
POST   /api/tickets/:id/resolve          # resolve --with (body: {"with": "..."})
```

**WebSocket frame** (pushed on every transition, regardless of source — PWA OR cron OR direct CLI on any agent's clone):

```json
{
  "type": "ticket-changed",
  "id": "T-NNNN",
  "lane": "<new-lane>",
  "actor": "operator" | "orchestrator" | "recovery" | "specialist" | "claude-fallback" | ...
}
```

### Backend shape

`kino-daemon` exposes the endpoints. Write-path implementation:

**Lean: shell-out to `tools/ticket.py`** (subprocess). Preserves the existing audit-chain artifact (commit + push cycle) since `ticket.py` already does git ops with `_git_utils` discipline (HERMES-guarded, README-rebuild, transition-log-append). Per-write subprocess overhead is acceptable for the expected per-second-or-less ticket transition rate.

**Alternative (NOT chosen for v0)**: in-process import of `tools/ticket._lifecycle` helpers. Faster (no subprocess overhead) but kino-daemon would need to do its own git commit + push, duplicating discipline (HERMES guard, README rebuild, transition log). If subprocess-overhead-per-write becomes load-bearing later (sustained >10 writes/sec — currently nowhere close), revisit.

### Filesystem watch for ticket-changed events

`kino-daemon` runs an inotify watch on `~/pantheon-config/tickets/{inbox,active,blocked,done}/`. On any add / move / delete event involving a `T-NNNN.md` file, daemon emits a `ticket-changed` WS frame to all connected PWA clients.

Captures all transition sources uniformly:
- PWA POSTs → `ticket.py` via shell-out → `git mv` → inotify fires
- Cron (Specialist's `watch_runner.py`, Recovery's `ticket_grooming.py` + `spine_index_groom.py`, etc.) → `git pull` → file changes on disk → inotify fires
- Direct CLI on any agent's clone → after `git push`, the clone-side watch missing it; OTHER clones get it via their next `git pull` per T-0120's polling-cron-pulls-first discipline

For the third case, kino-daemon's pantheon-config clone is itself a polling consumer; it pulls on a short interval (configurable; default 30s) so cross-agent transitions propagate within that window.

### Authentication + actor-attribution

Single-user via Tailscale identity headers per ORG-006 §Auth model. Tailscale tagged-node sets identity headers on incoming requests (exact header name + ID format follow Tailscale's documented identity-headers contract — implementer reads from Tailscale docs at integration time, not from this ADR); kino-daemon validates against the operator's configured identity and rejects others.

Every PWA-originated transition records `actor=operator` in the ticket's transition log (passed through `tools/ticket.py`'s `--actor` flag if available, otherwise via `PANTHEON_ACTOR` env var per cross-account routing pattern).

### Conflict handling

PWA writes go through the same `git mv` + auto-commit + push pipeline as `tools/ticket.py`. The pre-push hook (T-0147) auto-rebases on origin-divergence. If push collides at a filesystem-tier (two clones racing on the same `T-NNNN.md` rename), the second push gets a non-fast-forward error → `ticket.py` propagates the failure → kino-daemon returns 409 Conflict → PWA shows "refreshing — retry" + fetches origin + re-presents the (now-resolved) state. **No silent merging.**

### Reusable for future panes

The same contract generalizes to other pantheon-config artifact classes:

```
GET    /api/<resource>                   # list with filters
GET    /api/<resource>/:id               # detail
POST   /api/<resource>                   # create (where applicable)
POST   /api/<resource>/:id/<action>      # lifecycle action (where applicable)
WebSocket: {"type": "<resource>-changed", "id": "...", "lane?": "...", "actor?": "..."}
```

Future-pane mappings:

| Pane | Resource | Endpoints | WS event | Actions |
|---|---|---|---|---|
| Ticket interface (this ADR) | `tickets` | full set above | `ticket-changed` | claim / block / unblock / resolve |
| HANDOFF | `handoff` | GET only (read-only) | `handoff-changed` | none |
| Ledger | `ledger` | GET only | `ledger-changed` | none (append-only via existing tools) |
| ADR | `adr` | GET only | `adr-changed` | none (D2 hand-curated) |
| Lessons | `lessons` | GET only | `lessons-changed` | none (D2 hand-curated) |
| Incidents | `incidents` | GET only | `incidents-changed` | none (D2 hand-curated) |
| Principles + Directives | `principles`, `directives` | GET only | `<resource>-changed` | none (D2 hand-curated; the operator ratifies via PWA-eyeball + Telegram or direct edit) |

D2 protected resources are read-only via the API. PWA panes for those resources surface the structured-read view but never compose. Editing principles / directives / ADRs / hand-curated docs stays HO-or-operator-only via direct file edits.

### Scope (this ADR, v0)

In:

- The 6 HTTP endpoints + WebSocket frame for the `tickets` resource
- Filesystem-watch via inotify (`watchdog` Python library OR `inotifywait` subprocess; kino-daemon implementer picks at brief-tier per P1)
- Shell-out write path (subprocess `tools/ticket.py`)
- Tailscale identity gate
- Conflict handling via `ticket.py`'s existing semantics + 409 propagation

Out (deferred):

- Bulk operations (multi-ticket actions in one request)
- Label-edit (changing `labels:` field on existing tickets without going through full lifecycle)
- Multi-user / role-based access (single-user model holds per ORG-006 §Auth)
- Direct ledger-entry compose (different API; file separately if needed)
- ADR / principles / directives editing (D2-protected; never PWA-side)
- Telegram → kino historical-message migration (kino is forward-only)

## Alternatives considered

### A. In-process import of `tools/ticket._lifecycle` (no subprocess)

Faster per-write (no subprocess fork). But kino-daemon would need to:
- Re-implement git commit + push
- Re-implement HERMES guard
- Re-implement README rebuild
- Re-implement transition-log append

That's `tools/ticket.py`'s logic duplicated in kino-daemon. P5 violation (roles don't hop): `ticket.py` IS the lifecycle source-of-truth; kino-daemon is the API surface. Subprocess preserves the role boundary.

Revisit if/when sustained-write-rate becomes load-bearing.

### B. Direct git protocol (no `ticket.py` involvement)

Have the PWA push directly to origin via JSON-encoded `git mv` operations. Even worse than (A) — bypasses ALL of `ticket.py`'s semantic enforcement (frontmatter validation, transition log shape, README index rebuild). Reject.

### C. Polling-only refresh (no WebSocket)

PWA polls `GET /api/tickets` on an interval. Simpler; no inotify, no WebSocket. But: bad UX for the cross-agent transition case (latency = poll interval), and cron-side transitions won't propagate to open panes promptly.

WebSocket cost is bounded (~tens of bytes per frame; rare events). Reject (C); WS justifies the complexity for the multi-agent multi-pane reality.

### D. Hand-author per-pane API endpoints without a shared contract

What we'd do without this ADR. Each future pane authors its own routes ad-hoc. Predictable drift; harder for future-HO to grok the API surface; harder for PWA frontend to share fetch helpers across panes. Reject (D); this ADR exists to prevent it.

## Consequences

**Positive:**

- **Reduces D1 bandwidth burn**: the operator works the inbox queue from PWA (mobile + web) rather than round-tripping prose decisions through Telegram. The b33 cycle alone showed ~20 round-trips that PWA-ticket-actions would have collapsed into status-flips + comments.
- **Audit-chain unchanged**: shell-out preserves `ticket.py`'s existing commit + push + transition-log discipline. PWA writes are indistinguishable from CLI writes in `git log`.
- **Reusable contract**: future PWA panes (HANDOFF, ledger, ADR, lessons, incidents) inherit shape; less per-pane design work.
- **Cross-agent visibility**: WebSocket frames + filesystem-watch propagate cron / agent / CLI transitions to open PWA panes uniformly.

**Negative:**

- **Subprocess overhead per write**: ~100ms+ fork + Python startup + git ops. Not load-bearing at expected rates but a cap on throughput.
- **Inotify fragility**: filesystem watches can miss events under heavy load or race conditions. Mitigation: kino-daemon's own polling-pull on 30s interval as backup; UI shows "refreshing" state during reconcile.
- **Tailscale identity header is the SOLE auth gate**: if the header gets spoofed (e.g., reverse-proxy misconfiguration), the PWA inherits whatever identity the upstream sets. Must verify Tailscale identity is set at the Tailscale-tagged-node layer + propagated unchanged through kino-daemon's reverse proxy (if any).

**Open questions**:

- **Should `block` and `unblock` be in MVP or follow-up?** T-0144 said defer to follow-up; this ADR includes them in the API surface (cheap to add at API tier — same shell-out shape) but UI MVP can defer their buttons until a follow-up cycle. Lean: include in API; defer in UI per T-0144's MVP cut.
- **Filesystem-watch implementation**: `watchdog` Python library OR `inotifywait` subprocess. Defer to T-0170 implementer cycle; both are reasonable; the implementer picks with justifies-via.
- **Subprocess-overhead breakeven**: at what write-rate does in-process-import (alternative A) become worthwhile? Currently <1 write/min observed; no urgency. Re-evaluate if PWA usage drives sustained >10 writes/sec.

## Cross-references

- **ORG-006** — kino PWA spec (parent architecture)
- **ORG-007** — kino ↔ claude wire protocol (different surface; long-running stream-json process per agent; this ADR is the OTHER side, the operator-facing API)
- **ORG-003** — ticket system (canonical lifecycle source-of-truth; `tools/ticket.py` implements)
- **T-0144** (parent) — kino PWA Phase-2.x ticket interface + emoji support
- **T-0169** (this ADR's tracking ticket) — ORG-NNN ADR for kino PWA ticket-API contract; HO-direct authoring per D2
- **T-0170** (sibling, blocked-on-this-ADR) — implementer cycle for ticket interface (backend HTTP + WebSocket + filesystem-watch + write-path; MVP cut: read-list + read-detail + file-new + claim/resolve)
- **T-0171** (sibling, independent) — implementer cycle for emoji support (render + compose; defer reactions)
- `tools/ticket.py` — lifecycle source-of-truth that the API shells out to
- `tools/_git_utils.py` — shared git helpers (HERMES guard, fail-open pull); inherited by `ticket.py`'s commit path
- T-0147 — pre-push freshness hook (mitigates ID-collision class on kino-daemon's auto-rebase pull cycle)

## Authoring + ratification trail

- 2026-05-09 16:46 [operator via Recovery] T-0144 filed (parent ticket)
- 2026-05-09 23:33 [operator msg_838] T-0144 sub-decisions ratified (3 fan-out tickets directed: ADR + ticket-interface + emoji)
- 2026-05-10 01:58 [orchestrator] T-0169 (this ADR's tracking ticket) filed in inbox
- 2026-05-10 02:13 [orchestrator] ORG-009 authored as draft per T-0169 — HO-direct per D2 (hand-curated; HO authors + the operator ratifies)
- (pending) [operator] ratification — flips status `Authored` → `Ratified: <date> by the operator`
