---
aliases: [ORG-019]
title: Retire `tickets/blocked/` as a state; blocks expressed via `not_before` or `depends_on` only
status: Ratified 2026-05-19
date: 2026-05-18
build_disposition: built
---
# ORG-019 — Retire `tickets/blocked/` as a state; blocks expressed via `not_before` or `depends_on` only

**Status:** Drafted 2026-05-18 by Orchestrator (Orchestrator). **Ratified 2026-05-19T00:58Z by the operator via T-0454 (msg_1575 "ratify ORG-019").** Replaces the soft-block discipline accreted across T-0383, T-0439, T-0440, T-0452. Phase 2-4 execution authorized to HO.

**Date authored:** 2026-05-18

**Author:** Orchestrator (Orchestrator) — crystallizing the operator msg_1565+1567 verbatim.

**Related:** ORG-003 (ticket system), ORG-013 (relational triggers on ticket resolve — this ADR amends it), T-0439 (block discipline introduction), T-0440 (blocked_reassessment cron — this ADR retires it), T-0452 (OVERDUE flag — this ADR eliminates it), msg_1565+1567 (the operator spec).

**HITL:** Authored; ratified 2026-05-19 by the operator via T-0454.

## Context

The ticket system (ORG-003) currently supports four states: `inbox/`, `active/`, `blocked/`, `done/`. The `blocked/` state was added so operators could mark tickets waiting for unstructured triggers — phrases like "HO has capacity," "after PWA stats verified," "when echo-prismatic Phase 2 opens."

Over 2026-05-12 → 2026-05-18 the soft-block discipline accreted mechanisms:

- **T-0383** (2026-05-13) — blocked-by category vocabulary (`operator`/`orchestrator`/`specialist`/`recovery`/`event`/`external`).
- **T-0439** (2026-05-18) — `--unblock-when "<phrase>"` and `--unblock-check "<shell-cmd>"` flags on `ticket.py block`.
- **T-0440** (2026-05-18) — `tools/blocked_reassessment.py` cron firing every 30min; cascade-backfill, unblock_check exec, and surface routing.
- **T-0452** (2026-05-18) — OVERDUE flag parsing past-date triggers from `unblock_when` phrases.
- **Recovery's 2026-05-18 sharpening pass** — 20 of 23 blocked tickets re-written with hard-date `unblock_when` triggers ("After 2026-05-23 OR HO claims for ...").

The operator's response to the accretion (msg_1565+1567):

> "I'm thinking we just don't need a blocked section. It's causing some serious *ahem* blockage"
>
> "I mean I see no point in it at this juncture. a ticket can be marked as blocked on a timestamp or another ticket, but nothing else."

The substrate is the problem. Blocking is two real things:

1. **Calendar-driven wait** — "this can't fire until date X." Already expressible as `not_before: <ISO timestamp>` in frontmatter (T-0312 schema).
2. **Ticket-dependency wait** — "this can't fire until ticket Y resolves." Already expressible as `depends_on: [T-NNNN, ...]` in frontmatter (ORG-013 + T-0328 cascade-unblock).

Anything else — "HO has capacity," "the operator surfaces," "echo Phase 2 opens" — is a soft phrase the substrate can't structurally check. The blocked-reassessment cron papered over this by surfacing the soft phrases to the operator pane, but the operator still has to read them, judge them, and act on them. That's prose discipline pretending to be substrate.

## Decision

Retire `tickets/blocked/` as a state. Express blocks via the two structural fields only:

- `not_before: <ISO timestamp>` — calendar wait
- `depends_on: [T-NNNN, ...]` — dependency wait

**Nothing else.** Soft `unblock_when` phrases without a timestamp or dep-id are eliminated as a block mechanism.

A ticket waiting on a calendar gate or a dep stays in `inbox/`. The existing `inbox_consumer.py` filter already suppresses inbox tickets with future `not_before`. The ORG-013 cascade-unblock already removes `depends_on` gates when deps resolve. Both mechanisms work in `inbox/` — no separate state needed.

Tickets with no `not_before` and no `depends_on` are NEVER blocked. If they're not immediately actionable, they're either:
- **resolved as no-longer-relevant** (operator judgment), or
- **claimed into `active/`** for explicit work (even slow work).

The middle category — "this matters but isn't fireable yet for unstructured reasons" — does not exist in the post-amendment substrate. Operators name a date, name a ticket dep, or commit to work.

## Forces

- **P1 (architecture over policy)** — soft-block phrases are prose discipline pretending to be substrate. The cron papered over the gap; the gap was real.
- **P5 (roles don't hop)** — operator judgment doesn't belong in cron surfacing; it belongs at claim-time. Eliminating the soft-block path removes the spurious surface.
- **D1 (the operator's bandwidth)** — accreting mechanisms (T-0383 → T-0439 → T-0440 → T-0452) burns design bandwidth on a substrate that doesn't pay back. Retire and reclaim.
- **D2 (audit-chain integrity)** — fewer states → cleaner audit-chain. Resolve transitions and re-claims are the operations operators actually do.
- **D10 (context-frugality)** — 23 blocked tickets surfacing every 30min via journal + pane is operator-noise. Retiring eliminates the noise category.
- **Recovery's calibration observation (T-0453 body)** — "when you're adding multiple amendments to make a substrate work, the substrate may be the problem." Captures a real lesson: accretion of mechanisms around a friction point should trigger a substrate-review, not another mechanism.

## Alternatives considered

- **(A) Keep blocked/, harden the cron with telegram escalation for past-date** — T-0452 §4 path. **Rejected** by the operator's "nothing else" hard line and the underlying observation that soft phrases don't belong as block mechanisms regardless of escalation tooling.
- **(B) Keep `unblock_when` phrases as informational metadata only (no surface, no cron)** — would preserve transition-log context without the substrate enforcement. **Rejected** — half-measure; if the phrase isn't actionable it shouldn't drive any substrate behavior.
- **(C) Keep blocked/ for ratification-only blocks (the operator's queue)** — distinguishes operator-input-required from soft-blocks. **Rejected** — the operator's input is a `depends_on` on a ratify-ticket OR a `not_before` for a calendar gate; the same two fields cover it.
- **(D) Selected — full retirement.** Cleanest. Substrate-shape matches the operator's spec.

## Consequences

### State machine

Before:
```
inbox/ → active/ → done/
   ↓        ↑
blocked/ ←
```

After:
```
inbox/ → active/ → done/
        (with not_before suppression at inbox-consumer
         and depends_on cascade unblock; both fields
         live in frontmatter, no separate state needed)
```

### Code changes

**`tools/ticket.py`** — `block` subcommand semantics change. Two options on the table for the implementation phase:

- **(a)** Remove `block` entirely; add `defer --not-before <date>` and `dep --depends-on T-NNNN` as in-place frontmatter editors. Cleaner shape; breaks any external callers.
- **(b)** Keep `block` as a rename for setting one or both of those fields (drops `--blocked-by`, `--unblock-when`, `--unblock-check`, `--reason`). Less breakage; semantics still consistent with the new discipline.

HO picks (b) for the implementation phase per D10 (minimum disruption to existing call sites).

**`tools/blocked_reassessment.py`** — RETIRES entirely:
- Cascade-backfill folds into `inbox_consumer.py`'s depends_on cascade-unblock check (same logic, different home).
- `unblock_check` exec path — ELIMINATED per the operator's spec.
- OVERDUE flag (T-0452) — ELIMINATED.
- Surface routing — ELIMINATED.
- Systemd timer + service unit — UNINSTALL.

**`tools/ticket_grooming.py`** — drops blocked-revisit section. Grooming walks `inbox/` + `active/` only.

**`tools/inbox_consumer.py`** — confirm `not_before` suppression + `depends_on` cascade cover the new cases (likely already do; verify at implementation time).

### ORG-013 amendment

ORG-013's "auto-unblock cascade" semantics shift from `blocked → active` (move file between dirs) to `inbox` `depends_on` gate-removal (rewrite frontmatter when deps resolve; ticket file stays in `inbox/`). The cascade is still atomic with the parent resolve, still operator-overrideable, still race-safe.

ORG-013's "blocked-revisit digest" section retires entirely — there's no blocked/ to revisit.

ORG-013 will be amended in a follow-up commit alongside this ADR's ratification.

### Persona briefs (HO/Specialist/Recovery)

`/home/claude/CLAUDE.md` and Specialist/Recovery's session-anchor files have block-discipline sections. Amended to:

> **Block discipline (post-ORG-019):** A ticket waits via `not_before: <ISO ts>` or `depends_on: [T-NNNN]`. No other block mechanism exists. If a ticket is "blocked because [unstructured reason]," it is either resolved (work no longer relevant) or claimed into active/ (slow work counts). The `tickets/blocked/` directory does not exist.

### `rulebook/policies/trust-posture.md` + `sop/standard/*`

Sweep for references to blocked-state. Updates land in the implementation phase.

### Migration of currently-blocked tickets

23 tickets currently in `tickets/blocked/`. Per-ticket classification:

- **Has `depends_on` with at least one dep not yet in done/** → migrate to `inbox/`; cascade-unblock handles the rest (frontmatter unchanged).
- **Has `not_before` future** → migrate to `inbox/`; inbox_consumer suppresses until date crosses (frontmatter unchanged).
- **Has only soft `unblock_when` with a parseable ISO date** → convert phrase to `not_before: <ISO ts>` (e.g., "After 2026-05-23 OR HO claims" → `not_before: 2026-05-23T00:00:00Z`); migrate to `inbox/`.
- **Has only soft `unblock_when` with no parseable date** → operator-driven re-classification per ticket:
  - Cap-hit family (T-0041/0067/0155): the `unblock_check` on sr_log mtime is essentially an event-gate; can be reformulated as `not_before: <ISO ts>` pegged to last-known-session-reset OR moved to active/ for explicit cap-hit-handling work.
  - HO-pickup tier ("HO has capacity for X"): claim into active/. The work is real; slow is fine.
  - Truly stale ("not sure if this still matters"): resolve as no-longer-relevant.
- **Has neither `not_before` nor `depends_on` nor parseable `unblock_when`** → operator judgment: resolve or claim active/.

Drop `blocked_by` and `unblock_when` and `unblock_check` fields from frontmatter at migration time (no longer schema-recognized).

### Ratification scope

The operator ratifies via **T-0454** (sibling ratify-ticket; target=operator; ratifiable_file points at this ADR). The ratification covers:

1. This ADR (ORG-019).
2. The migration plan as specified above.
3. Authorization for HO to execute Phases 2-4 below.

Phase 1 (draft) closes when the operator ratifies T-0454; Phase 2-4 execute via HO under the ratified scope.

## Implementation phases

**Phase 1 — Draft + ratification** *(this ADR)*:
- ✅ ORG-019 drafted (this file).
- ✅ T-0454 filed (target=operator; ratifiable_file=adr/ORG-019-blocked-state-retirement.md).
- ⏳ the operator ratifies T-0454 → triggers cascade-unblock on T-0453 → Phase 2 begins.

**Phase 2 — Substrate code changes** *(HO executes post-ratify)*:
- `tools/ticket.py`: `block` subcommand simplified per Option (b) above.
- `tools/blocked_reassessment.py`: retire entirely.
- `tools/ticket_grooming.py`: drop blocked-revisit section.
- `tools/inbox_consumer.py`: verify not_before suppression + cascade-unblock cover new cases.
- Persona briefs (`/home/claude/CLAUDE.md` for HO, Specialist/Recovery session-anchors): block-discipline section amended.
- `rulebook/policies/trust-posture.md` + `sop/standard/*`: sweep + update.
- ORG-013 amendment landed in same PR.

**Phase 3 — Migration** *(HO executes post-substrate)*:
- Walk current `tickets/blocked/*.md` (23 tickets); per-ticket classify per matrix above.
- `git mv tickets/blocked/T-NNNN.md tickets/inbox/T-NNNN.md` (or `active/`); strip removed frontmatter fields.
- `rmdir tickets/blocked/`.
- Single migration commit.

**Phase 4 — Verify + close**:
- No agent code references `blocked/`.
- Systemd timer/service for `pantheon-blocked-reassessment` is `systemctl --user disable + stop` and unit files removed.
- Next day's grooming digest shows healthy `inbox/` + `active/` only.
- T-0453 resolves.

## Cross-references

- ORG-003 — ticket system (the substrate this amends)
- ORG-013 — relational triggers on ticket resolve (this ADR amends; cascade-unblock semantics shift from `blocked → active` to inbox-frontmatter-gate-removal)
- ORG-015 — ratifiable-document-class allowlist (this ADR's ratification path)
- T-0439 — block discipline introduction (the substrate this retires)
- T-0440 — blocked_reassessment cron (retires)
- T-0452 — OVERDUE flag (eliminated by retirement)
- T-0453 — parent ticket (this draft satisfies Phase 1; cascade-unblocks on T-0454)
- T-0454 — the operator's ratify ticket
- msg_1565 + msg_1567 — the operator's spec (verbatim quoted in Context)

---

**Status footer:** Drafted 2026-05-18 ~10:15Z. Ratified 2026-05-19 by the operator via T-0454 (msg_1575). Phase 2 substrate change landed at pantheon-config agent/T-0453-phase2 (commits 9f2dc63, c06b177, 1634406, e8720be). Phase 3 (migration) + Phase 4 (verify + close) follow.
