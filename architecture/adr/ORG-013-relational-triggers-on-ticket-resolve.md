---
aliases: [ORG-013]
title: Relational triggers on ticket resolve (auto-unblock cascade + blocked-revisit digest)
status: Ratified 2026-06-07 by the operator (authored 2026-05-14)
date: 2026-05-14
build_disposition: built
---
# ORG-013 — Relational triggers on ticket resolve (auto-unblock cascade + blocked-revisit digest)

**Status:** **Ratified 2026-06-07 by the operator.** Authored 2026-05-14; Implementation already landed at pantheon-config@107301a (T-0328); this ADR captures the decisions that shaped that implementation per the operator verdict msg_~1497-window ("this should be an adr not an sop"). Replaces draft SOP `sop/standard/relational-triggers-on-ticket-resolve.md` (Recovery-authored 2026-05-14; retired in favor of this ADR per [[ORG-012]] §Promotion #5 SOP→ADR conversion path — architectural-decision-among-alternatives with named tradeoffs is ADR-shape, not SOP-shape).

**Amended 2026-05-19 by ORG-019:** `blocked/` directory retired. Cascade-unblock semantics shift from "blocked → inbox/active dir move" to "dependant in inbox/ with `depends_on` gate clears when last dep resolves (frontmatter rewrite; file stays in inbox/)." Blocked-revisit digest section retires entirely. See §1 (amended) and §2 (retired) below for the post-ORG-019 mechanism shape.

**Amended 2026-09-22 by T-3933** (orchestrator-autonomous per infra-design msg10007 + goal-neutral maintenance; **Adopted 2026-09-27 under D3**: cascade mechanics sit below the goal-level bar and the operator's msg_1293 unblock gate is unchanged, so no ratify walk is owed (T-4000)): cascade splits into two legs — a **partial-strip** leg (always fires on every resolve, prunes `parent_tid` and any independently-shipped sibling from every dependant's `depends_on`; no unblock, no stamp changes) plus the historical **full-unblock** leg (fires only when `_all_other_deps_shipped`; strips `auto_delivered` stamps; makes the dependant deliverable). This is a strict refinement, NOT a reversal of the operator's msg_1293 ratification: the full-unblock leg's gate + stamp semantics are byte-unchanged. What T-3933 closes is the middle-state accumulation gap the original hard-contract exposed — a dependant with `depends_on: [A(done), B(open)]` used to keep A as a stale-done ref indefinitely (or until every sibling shipped, or until a hand/sweep cleanup ran). Now A gets pruned eagerly; B still gates. See §1 (updated hard-contract) below.

**Amended 2026-09-29 by T-4186** (orchestrator-autonomous, on the same basis as T-3933: cascade mechanics sit below the goal-level bar; the operator's msg_1293 unblock gate and both legs' semantics are unchanged, so no ratify walk is owed). The edge-triggered **PR-merge path is retired**, and a **level-triggered backstop sweep** replaces it. Resolve-time cascade is unchanged and remains primary.
- `.github/workflows/cascade-on-merge.yml` (T-3289) walked each push's renames into done/ and ran `ticket.py cascade --tid` for each. Its `GITHUB_TOKEN` could never push to protect-main (the bypass actors are DeployKey and User only, and there were 0 `cascade:` commits ever), and stale refs had already leaked (T-3382 on T-3381; T-3227 on T-3176 and T-3218).
- `tools/cascade_sweep.py` on a 15-minute seat-side timer (`pantheon-cascade-sweep.{service,timer}`, on the claude seat) finds every inbox/active ticket whose `depends_on` names a shipped tid (`_dep_is_shipped`, ORG-089). It then runs the same `ticket.py cascade --tid`, under the existing lock/pull/push path.
- It is **additive**, not a reversal of "Atomic commit over event+cron pickup". Resolve still cascades atomically in the resolve commit. The sweep only reconciles tickets that reached done/ by a non-resolve route (a PR/admin merge move, or a hand move) or were missed. It is idempotent and makes no commit when nothing is stale.
- `cmd_cascade` now also **refuses a dropped tid** (a `_dep_is_shipped` guard, pre-lock and in-lock). Before this, a manual `cascade --tid <dropped>` would have stripped a dependant's gate on a bet that was abandoned, not shipped (ORG-089).

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

`tools/ticket.py resolve T-X` — after moving T-X to `done/` and editing its frontmatter, `_cascade_unblock_dependants(T-X)` runs as a `cascade_callback` inside `_do_transition` (before the commit). The cascade fires two legs, per T-3933:

1. Scans `inbox/`, `active/` for tickets whose `depends_on` includes T-X.
2. For each dependant, computes `remaining_deps = [d for d in deps if d != T-X and not _dep_is_shipped(d)]` and rewrites the dependant's `depends_on` to `remaining_deps` — pruning T-X **plus any other independently-shipped sibling** (T-2177). This step is **always applied** (partial-strip leg).
3. **If** every other dep was already shipped (`_all_other_deps_shipped`): also strips `auto_delivered` / `auto_delivered_at` stamps (T-0971 arm-A) and appends `[auto-unblock] dep resolved` to the dependant's transition log — the ticket is now deliverable (full-unblock leg).
4. **Else** (dependant still gated on at least one open sibling): appends `[cascade] stale-done dep <T-X> stripped` to the transition log, no stamp changes. The dependant stays in inbox/+active/, `depends_on` faithfully reflects only the still-open gates.
5. All cascade file changes batch into the SAME atomic commit as the parent resolve. Commit message (via `cmd_cascade`, the sweep/manual path since T-4186; the retired PR-merge Action before it) distinguishes the legs: `"ORG-013 cascade (T-3933 two-leg): N partial-strip (stale-done ref pruned; still gated) + M auto-unblock (fully deliverable now)"` (T-3941).

Tickets with future `not_before` stay suppressed by `inbox_consumer.py` regardless of cascade activity — the calendar gate and the dependency gate are orthogonal mechanisms.

**Hard contract (load-bearing; T-3933-refined):**
- **Always-on.** No opt-in/opt-out label; the relational trigger is the default mechanism.
- **Partial-strip always fires** (T-3933). Every resolve prunes the resolving tid — plus any independently-shipped sibling — from every dependant's `depends_on`. Closes the middle-state accumulation gap the original all-or-nothing wording produced.
- **Full-unblock (deliverable-status change + stamp-strip) still fires only on all-deps-shipped** — the operator msg_1293's original ratification is preserved byte-for-byte on this axis. A dependant with a still-open sibling gets its stale-done refs pruned but stays gated.
- **Malformed dep IDs fail closed on the UNBLOCK path only.** A dependant with a non-`T-NNNN`-shaped or dropped sibling stays "not-yet-deliverable" — the unblock gate refuses to reason about uncertainty. The partial-strip leg is NOT gated by that fail-closed posture (T-3933 decision i): a malformed sibling says nothing about whether the just-shipped parent should still be listed; the shared `remaining_deps` computation drops parent unconditionally while a malformed sibling stays on its own account.

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

**T-3933 partial-strip leg (landed at pantheon-config@a559ff4d3, PR #471 merged 2026-09-22):**
- `_cascade_unblock_dependants` split into a shared `remaining_deps` computation + a conditional `if full_unblock:` branch for the deliverable-status change (stamps + `[auto-unblock]` actor). The `else:` branch appends `[cascade] stale-done dep <tid> stripped` and touches no stamps.
- `_all_other_deps_shipped` (renamed from `_all_other_deps_done` per T-3440 Scope B) — unchanged; the fail-closed unblock gate is verbatim from msg_1293.
- Tests: `test_ticket.py:TestCascadeUnblockOnResolve` (399 cases across the module), `test_ticket_cascade.py:TestCmdCascadeMultiDepMixedState` + `TestCmdCascadeLegAwareCommitMessage` (T-3941 leg-aware commit-body coverage), `test_appetite_circuit_breaker.py:TestDroppedDepDoesNotUnblock` (FALSIFIER 2 dropped-sibling still gates).
- 6 line-targeted mutations (M1-M6, T-3933 impl-side) confirm falsifiability. M4 substituted with M4′ (partial leg drops only parent_tid) after original M4 discovered to be a no-op given call-site invariants — disclosed at commit time per feedback_no_fabricated_tool_results.

**T-3941 leg-aware commit-message body (landed at pantheon-config@4bfe013d1):**
- `_cascade_unblock_dependants` grew an opt-in `stats: dict[str, int] | None = None` param.
- `cmd_cascade` opts in + builds the commit-message body from per-leg counts so `git log` distinguishes partial-strip-only from full-unblock fires.

**T-4186 level-triggered sweep (landed at pantheon-config@1986ecfde, PR #529, merged 2026-09-29):**
- `tools/cascade_sweep.py` reconciles stale refs. It re-detects between tids, attempts each tid once, and caps at 25 per run.
  - DEFERRED (exit 0): a busy `git_mutation_lock`, or the seat checkout on a non-main branch.
  - FAILED (exit 1, `OnFailure` alert): non-convergence.
  - `--check` is read-only.
- `tools/systemd/pantheon-cascade-sweep.{service,timer}` runs at `*:04,19,34,49`, with `Persistent=true`, installed on the claude seat.
- Retired: `.github/workflows/cascade-on-merge.yml` and `tools/cascade_diff_scan.py`, plus their tests.
- `cmd_cascade` has the dropped-tid refusal, with each check pinned by its own test.
- Tests: `tools/test_cascade_sweep.py`, including a hermetic real-git e2e with a racing push, and `tools/test_ticket_cascade.py`.
- Live canary at install (2026-09-29 ~02:49Z): `--check` listed the 3 leaked refs (T-3176, T-3218 → T-3227; T-3381 → T-3382). The first sweep reported fixed=2, skipped=1 (T-3218 was cleared as a sibling side effect of T-3176), with 2 `cascade:` commits (14ea796a4, b1e788c9b); T-3227 is now `depends_on: [T-3226]` and T-3382 `[T-2667]`. The second sweep was a no-op.
- Liveness and deferral monitoring: T-4220 (PR #538, merged @ae1be4ffa, 2026-09-29).
  - The 8th CONSECUTIVE pure-deferral run exits 1, so `OnFailure` pages. That narrows "DEFERRED (exit 0)" above to a single or short run of deferrals. The counter is fail-open and never changes the exit code on its own faults.
  - The consumer-liveness monitor (Recovery's seat) watches the sweep for fires-then-goes-quiet: 60 min max silence, via its `WATCHED_UNITS` registry.
  - A deleted or disabled sweep timer is report-only there. Ownership of that gap is T-4232.

## Re-evaluate when

- Manual-block usage emerges as a regular pattern → reconsider Option B (opt-out label).
- Cross-repo `depends_on` becomes a thing → cascade currently only scans `pantheon-config/tickets/`; cross-repo would need a different lookup mechanism (and probably a sibling ADR).
- Cascade firing rate becomes load-significant → already batched (one dep-scan per resolve); further optimization (cached dep-graph) only justified if `_find_dependants` shows up in profiles.
- The sweep's fix rate stays well above 0 (T-4186) → something is routing tickets to done/ outside resolve. Find it, rather than leaning on the backstop.

## DECISIONS

- **Always-on cascade over opt-in/opt-out label** — justifies via P1 + the operator msg_1293 (default-on; rare override case bears the friction).
- **Atomic commit over event+cron pickup** — justifies via D2 (audit-chain integrity; no inbox/blocked-mismatch window between resolve and unblock).
- **All-deps-done firing over partial-cascade** — justifies via D10 + clarity-of-state (`depends_on` showing un-done IDs is a clearer "still waiting" signal than a half-unblocked state). *(T-3933 refinement: the deliverable-status change (unblock + stamp-strip) still fires only on all-deps-done. The complementary partial-strip leg keeps `depends_on` faithful to actually-open gates without changing deliverability — the two axes are separable and had been conflated in the original wording. See below.)*
- **Prior-claim-aware restore-target over always-inbox** — justifies via mid-work-preservation (a previously-claimed ticket carries work context that returning-to-inbox would lose).
- **Malformed-dep fail-closed on unblock** — justifies via D2 + P1 (failure mode should be visible-blocked-for-review, not silent-auto-unblock on uncertainty). *(T-3933: the fail-closed posture gates deliverability only. Partial-strip prunes the just-shipped parent regardless — a malformed sibling says nothing about whether the parent should still be listed.)*
- **T-3933 partial-strip refinement (added 2026-09-22)** — justifies via P1 (mechanism-over-convention: structural prune retires the periodic-sweep alternative) + P4 (traceability: `depends_on` reflects actually-open gates instead of accumulating stale-done refs until every sibling ships) + D6 (minimum-viable-change: extends the existing cascade, adds no new scheduled unit or CLI surface) + D10 (compatibility: msg_1293's all-deps-done unblock gate is byte-unchanged; partial-strip is strictly additive).
- **T-4186 level-triggered sweep over the edge-triggered PR-merge Action (added 2026-09-29)** — justifies via P1 (a reconcile that re-reads state converges whatever route moved a ticket to done/; the edge trigger could never push and silently leaked 3 refs) + D2 (the resolve-time atomic cascade stays primary; the sweep's own `cascade:` commits are traceable) + P7 (cheap backstop: idempotent, no commit when clean). The dropped-tid refusal in `cmd_cascade` justifies via ORG-089 (dropped ≠ shipped).
- **SOP→ADR conversion** — justifies via [[ORG-012]] §Promotion #5 (architectural decision-among-alternatives with named tradeoffs is ADR-shape, not SOP-shape) + the operator verdict msg_~1497-window ("this should be an adr not an sop").

## Cross-references

- ORG-003 — ticket system this ADR extends.
- [[ORG-012]] §Promotion #5 — SOP→ADR conversion path justifying this restructure.
- T-0185 (transition log 2026-05-13T06:17Z) — external-substrate gap incident motivating the manual-block override preservation.
- T-0244 — `git pull --ff-only` discipline that bounds cross-process race-safety for the cascade.
- T-0328 — implementation ticket (closed); T-0329 — ratification ticket (was for the retired SOP; superseded: ORG-013 ratified directly 2026-06-07). *(Erratum 2026-09-27, operator-approved TG 10399 / T-4036.)*
- T-3182 — artisan's arc where the middle-state accumulation gap surfaced (2 of 4 dependants hand-cleared after resolve; post-cascade hygiene sweep @34332a9f had independently cleaned 20 more stale-done refs). Motivated T-3933.
- T-3933 — partial-strip leg implementation (PR #471 merged @a559ff4d3, 2026-09-22).
- T-3941 — leg-aware commit-message body (landed @4bfe013d1).
- T-3942 — this amendment (Adopted 2026-09-27 under D3; T-4000).
- T-4186 / T-4199 — level-triggered sweep replacing the cascade-on-merge Action (PR #529 merged @1986ecfde, 2026-09-29); T-4220 — its liveness + deferral escalation.
- `lessons/structural-enforcement-retires-the-prose-rule.md` — same P1-flavor lesson this ADR operationalizes for `depends_on` tracking.
- `lessons/2026-09-22-adversarial-review-plus-mutation-test-falsification.md` — falsifiability discipline the T-3933 mutation coverage carries forward.
