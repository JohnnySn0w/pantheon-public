---
aliases: [ORG-013]
title: Relational triggers on ticket resolve (auto-unblock cascade + blocked-revisit digest)
status: Ratified 2026-06-07 by the operator (authored 2026-05-14)
date: 2026-05-14
---
# ORG-013 — Relational triggers on ticket resolve (auto-unblock cascade + blocked-revisit digest)

**Status:** **Ratified 2026-06-07 by the operator.** Authored 2026-05-14; Implementation already landed at pantheon-config@107301a (T-0328); this ADR captures the decisions that shaped that implementation per the operator verdict msg_~1497-window ("this should be an adr not an sop"). Replaces draft SOP `sop/standard/relational-triggers-on-ticket-resolve.md` (Recovery-authored 2026-05-14; retired in favor of this ADR per [[ORG-012]] §Promotion #5 SOP→ADR conversion path — architectural-decision-among-alternatives with named tradeoffs is ADR-shape, not SOP-shape).

**Amended 2026-05-19 by ORG-019:** `blocked/` directory retired. Cascade-unblock semantics shift from "blocked → inbox/active dir move" to "dependant in inbox/ with `depends_on` gate clears when last dep resolves (frontmatter rewrite; file stays in inbox/)." Blocked-revisit digest section retires entirely. See §1 (amended) and §2 (retired) below for the post-ORG-019 mechanism shape.

**Date authored:** 2026-05-14

**Author:** Orchestrator (Orchestrator) — restructured from Recovery's prior SOP draft.

**Related:** ORG-003 (ticket system), T-0328 (implementation), T-0329 (ratification), [[ORG-012]] §Promotion #5, T-0185 (external-substrate gap incident).

**HITL:** Authored

## Context

The Pantheon ticket system (ORG-003) tracks `depends_on` as a passive metadata field — a ticket filed with `depends_on: [T-X]` carries the linkage, but prior to T-0328 the parent's resolution did not automatically affect the dependant. Operators manually scanned + unblocked dependants when parents closed. Two failure modes followed:

1. **Stuck blocked tickets** that should have moved to inbox/active when the parent resolved — operator forgot; queue surface stale.
2. **Stale-after fire on dependants** whose parent already resolved — surfacing as "stale" in grooming digests but actually just waiting for an unblock that should have been automatic.

The proximate trigger is the operator's directive msg_1291 + msg_1293 (2026-05-13): "build the relational mechanism + auto-unblock on resolve cascade." The deeper context is the T-0185 incident (2026-05-13T06:17Z transition log) where Recovery's auto-unblock on T-0133 resolve fired without checking the external substrate condition — surfaced the gap between "dep satisfied" and "ticket actually unblockable."

## Forces

- **P1 (architecture over policy)** — prose discipline of "remember to unblock dependants on resolve" doesn't scale and drifts. Same lesson as [[ORG-011]] §Forces and `lessons/structural-enforcement-retires-the-prose-rule.md`.
- **D2 (audit-chain integrity)** — cascade actions must be traceable in the transition log + commit history; atomic with the parent resolve preferred so commit-history reads cleanly.
- **D10 (context-frugality)** — operator shouldn't have to scan `blocked/` after every resolve to find unblock candidates.
- **External-substrate gap** (T-0185 lesson) — "all deps done" does not always mean truly unblockable; external substrate conditions can lurk outside the dep graph. Mechanism must support operator override (manual block) without disabling the cascade globally.
- **Race-safety** — multi-process actors (HO + Recovery + Specialist on the same repo) must not corrupt state on concurrent resolves; existing `git pull --ff-only` discipline (T-0244) bounds cross-process races.

## Candidate mechanisms

**Option A — Opt-in via label** (e.g. `autounblock`): operator opts each dependant into the cascade.
- *Rejected*: most tickets benefit from cascade by default; opt-in inverts the cost (operator pays setup on every dependant; rare override case is the cheap one to make explicit).

**Option B — Opt-out via label** (e.g. `manual-hold`): cascade fires by default; label suppresses.
- *Deferred*: no manual-block usage pattern observed yet; revisit if patterns emerge (see §Re-evaluate). Manual block via `ticket.py block` already provides per-instance escape.

**Option C (chosen) — Always-on cascade with explicit manual-block override**: cascade fires on every resolve; operator override = use `ticket.py block --reason "..."` after auto-unblock fires.
- *Rationale*: matches the operator's msg_1293 framing; default-on inverts the cost correctly (rare override case bears the friction); manual-block is already part of the lifecycle vocabulary.

**Option D — Event-publish + separate-cron pickup**: resolve publishes an event; cron later scans + unblocks.
- *Rejected*: latency between resolve and unblock surfaces as inbox/blocked-mismatch during the gap. Atomic cascade preserves the invariant that `tickets/` filesystem state always matches the most recent commit.

**Option E — Eager partial-resolve cascade**: when ONE of multiple deps resolves, dependant moves to inbox.
- *Rejected*: half-unblocked-half-not is a confused state. The dependant's frontmatter `depends_on` still showing un-done IDs is a clearer "still waiting" signal than an inbox-resident-with-pending-deps state.

**Option F — Always restore to inbox** (no prior-claim awareness): cascade always unblocks into `inbox/`.
- *Rejected*: a ticket that was previously claimed-and-then-blocked is mid-work; restoring to `active/` preserves the work context. Restore-target via prior-claim transition-log scan is a small cost for the correct semantic.

## Decision

**Option C (always-on) + Option E rejection (all-deps-done firing condition) + Option D rejection (atomic commit) + Option F rejection (prior-claim-aware restore) + malformed-dep fail-closed.**

Mechanism details:

### 1. Auto-unblock cascade on resolve

**Post-ORG-019 semantics:** the cascade rewrites `depends_on` on dependant tickets in-place rather than moving files between directories.

`tools/ticket.py resolve T-X` — after moving T-X to `done/` and editing its frontmatter, `_cascade_unblock_dependants(T-X)` runs as a `cascade_callback` inside `_do_transition` (before the commit). The cascade:

1. Scans `inbox/`, `active/` for tickets whose `depends_on` includes T-X.
2. For each dependant: checks if every OTHER dep is also in `done/`.
3. If all deps satisfied: removes T-X from the dependant's `depends_on` list (frontmatter edit), appends a transition log entry: `[auto-unblock] depends_on T-X resolved (cascade)`. File stays in its current directory (no `git mv`).
4. All cascade file changes batch into the SAME atomic commit as the parent resolve. Commit message gains a trailing `Auto-unblock cascade: N dependant(s) had depends_on satisfied per T-0328.` line.

Tickets with future `not_before` stay suppressed by `inbox_consumer.py` regardless of cascade activity — the calendar gate and the dependency gate are orthogonal mechanisms.

**Hard contract (load-bearing):**
- **Always-on.** No opt-in/opt-out label; the relational trigger is the default mechanism.
- **Multi-dep partial-resolve does NOT fire** (frontmatter still shows other-deps in `depends_on`). Only fires on all-deps-done.
- **Malformed dep IDs fail closed.** A dependant with a non-`T-NNNN`-shaped dep stays "dep not done." Surfaces during grooming or human review rather than silently auto-unblocking on uncertainty.

### 2. ~~Blocked-revisit digest section~~ — RETIRED per ORG-019

The `tools/ticket_grooming.py` blocked-revisit digest section retires entirely with the `blocked/` state. Grooming walks `inbox/` + `active/` only post-ORG-019. The mechanisms that the digest used to surface — stuck-blocked-after-parent-resolve, external-dep-block — are no longer expressible as a separate state: a ticket waiting on something the cascade can't auto-resolve sits in `inbox/` with `depends_on: [T-NNNN, ...]` or `not_before: <ISO ts>`, and the `inbox_consumer` filter handles the surfacing.

## Consequences

**Positive:**
- Stuck-blocked-after-parent-resolve failure mode structurally retired.
- Atomic commit preserves audit-chain integrity (D2): no inbox/blocked-mismatch windows.
- Manual-block escape hatch preserves operator override capability (T-0185 lesson preserved).
- *(Post-ORG-019: `blocked/` is retired.)* A dependant stays in `inbox/` with a `depends_on: [T-NNNN, ...]` (or `not_before`) gate; the `inbox_consumer` suppresses pickup until the gate clears — the surface stops accumulating stale auto-correctable entries.

**Negative:**
- Cascade fires on every resolve, even when no dependants exist (small constant cost).
- Multi-dep dependants stay gated (in `inbox/` with `depends_on`, post-ORG-019) until the LAST dep resolves — operator sees the ticket still gated even when 4 of 5 deps are done. The cascade removes resolved tids from `depends_on` in-place; the `inbox_consumer` surfaces per-dep status (the blocked-revisit digest section that originally mitigated this is itself retired with `blocked/`).
- External-substrate conditions still require operator-explicit manual block; cascade can't detect "deps satisfied but the world outside the dep graph still says no."

**Deferred:**
- Opt-out label (Option B) — revisit when manual-block usage emerges as a regular pattern.
- Cross-repo `depends_on` — current cascade scans only `pantheon-config/tickets/`; cross-repo dep linkage would need a different lookup mechanism (and probably a different ADR).
- Eager partial-cascade — rejected here but not foreclosed; if multi-dep-tracking surfaces as an operator pain, the partial-state semantics deserve revisit.

**Open questions:**
- None currently. All design questions closed per the operator msg_1293 ratification of cascade-on-resolve.

## Implementation (landed at pantheon-config@107301a)

Atomic with this ADR's ratification — the code shipped via T-0328:
- `tools/ticket.py:_cascade_unblock_dependants` — cascade fn called via `_do_transition(cascade_callback=...)`.
- `tools/ticket.py:_find_dependants` / `_all_other_deps_done` / `_restore_state_for_dep` — supporting helpers.
- `tools/ticket_grooming.py:_scan_blocked_tickets` + `build_blocked_revisit_section` — daily digest path.
- Tests: `tools/test_ticket.py:TestCascadeUnblockOnResolve` (9 cases); `tools/test_ticket_grooming.py:TestBlockedRevisitSection` (5 cases).

Ratification gates this ADR's documentary precedent; code behavior is live regardless.

## Re-evaluate when

- Manual-block usage emerges as a regular pattern → reconsider Option B (opt-out label).
- Cross-repo `depends_on` becomes a thing → cascade currently only scans `pantheon-config/tickets/`; cross-repo would need a different lookup mechanism (and probably a sibling ADR).
- Cascade firing rate becomes load-significant → already batched (one dep-scan per resolve); further optimization (cached dep-graph) only justified if `_find_dependants` shows up in profiles.

## DECISIONS

- **Always-on cascade over opt-in/opt-out label** — justifies via P1 + the operator msg_1293 (default-on; rare override case bears the friction).
- **Atomic commit over event+cron pickup** — justifies via D2 (audit-chain integrity; no inbox/blocked-mismatch window between resolve and unblock).
- **All-deps-done firing over partial-cascade** — justifies via D10 + clarity-of-state (`depends_on` showing un-done IDs is a clearer "still waiting" signal than a half-unblocked state).
- **Prior-claim-aware restore-target over always-inbox** — justifies via mid-work-preservation (a previously-claimed ticket carries work context that returning-to-inbox would lose).
- **Malformed-dep fail-closed** — justifies via D2 + P1 (failure mode should be visible-blocked-for-review, not silent-auto-unblock on uncertainty).
- **SOP→ADR conversion** — justifies via [[ORG-012]] §Promotion #5 (architectural decision-among-alternatives with named tradeoffs is ADR-shape, not SOP-shape) + the operator verdict msg_~1497-window ("this should be an adr not an sop").

## Cross-references

- ORG-003 — ticket system this ADR extends.
- [[ORG-012]] §Promotion #5 — SOP→ADR conversion path justifying this restructure.
- T-0185 (transition log 2026-05-13T06:17Z) — external-substrate gap incident motivating the manual-block override preservation.
- T-0244 — `git pull --ff-only` discipline that bounds cross-process race-safety for the cascade.
- T-0328 — implementation ticket (closed); T-0329 — ratification ticket (was for the retired SOP; will be re-filed against this ADR).
- `lessons/structural-enforcement-retires-the-prose-rule.md` — same P1-flavor lesson this ADR operationalizes for `depends_on` tracking.
