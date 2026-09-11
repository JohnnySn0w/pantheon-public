---
aliases: [ORG-021]
title: "Pantheon ticket-state taxonomy: 3 directory states; substates-as-frontmatter"
status: Ratified 2026-06-07 by the operator (authored 2026-05-19)
date: 2026-05-19
build_disposition: built
---
# ORG-021 — Pantheon ticket-state taxonomy: 3 directory states; substates-as-frontmatter

**Status:** **Ratified 2026-06-07 by the operator.** Drafted 2026-05-19 by Orchestrator (Orchestrator).

**Date authored:** 2026-05-19

**Author:** Orchestrator (Orchestrator) — per T-0464 (Specialist's design-noodle output from T-0455 resolution).

**Related:** ORG-003 (ticket-system spec); ORG-013 (cascade-unblock on resolve, amended by ORG-019); ORG-015 (ratifiable-document-class allowlist); ORG-019 (blocked-state retirement — the substrate move this ADR formalizes-the-direction-of); T-0453 (the retirement cycle); T-0455 (Specialist's design-noodle that proposed this ADR); T-0464 (this ADR's authoring ticket); T-0465 (sibling — target=operator intent disambiguation; Part 1 substrate shipped 2026-05-19); [`lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md`](../lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md) (the ratified discipline this ADR locks-in-direction-for).

**HITL:** Authored 2026-05-19; ratified 2026-06-07 by the operator.

## Context

ORG-019 retired `tickets/blocked/` after a day of mechanism-stacking (T-0383 / T-0439 / T-0440 cron / T-0452 OVERDUE flag / Recovery's sharpening passes / memory pins) failed to make the soft-block discipline work. The operator's verdict msg_1565+1567: "blocked = timestamp OR depends_on, nothing else." The substrate move was compression: collapse a directory state into structured frontmatter fields.

Specialist's 2026-05-19 design-noodle (T-0455 → `watch/reports/2026-05-19/ticket-system-design-noodle.md`, commit 4e34398) named the recursive observation: the ticket system has accumulated ~14 amendments (T-0218 / T-0244 / T-0306 / T-0328 / T-0356 / T-0383-T-0453 blocked-arc / T-0454 / T-0455) — well past the stacking-lesson threshold ratified 2026-05-18. The substrate is mostly right; friction is at the cross-clone coordination + state-machine taxonomy layers, not the markdown+git substrate itself.

T-0464's ask: lock the compression direction explicitly via ADR so the next stuck-state shape doesn't drift the substrate back toward decomposition. Without an explicit commitment, a future actor faced with a new substate question (e.g., "should we add a `waiting-on-operator/` directory?") might re-create the blocked/ class of problems by re-decomposing.

## Decision

**Pantheon tickets have exactly 3 directory-tier states:**

- `tickets/inbox/` — filed, not yet claimed by an actor
- `tickets/active/` — claimed by an actor, in progress (or waiting on a structural gate, see substates below)
- `tickets/done/` — resolved

**Substates are encoded as frontmatter fields, NOT as new directories.**

The current substates (ratified by their authoring ADRs/SOPs):

- `depends_on: [T-NNNN, ...]` — ticket-dependency gate (ORG-013, amended by ORG-019); ticket is fireable only when all deps resolve
- `not_before: <ISO timestamp>` — calendar gate (ORG-019); ticket is fireable only when timestamp has passed
- `ratifiable_files: [<repo>:<path>, ...]` — ratification target (ORG-015); identifies this ticket as a ratify-request for the named files
- `labels: [interactive-required, ...]` — inbox-consumer-pickup suppression; structurally signals "needs operator judgment"
- `target: <actor>` — routing (ORG-003); names the actor expected to act on the ticket
- `requester: <actor>` — provenance (ORG-003); names the actor who filed
- `parent_ticket: T-NNNN` — lineage (ORG-003); names the ticket this one was filed from
- `stale_after: <duration>` — grooming gate (T-0235); used by `ticket_grooming.py` to flag tickets that have sat too long
- `intent: action|ratify|noodle` — target=operator queue disambiguation (T-0465, Part 1 substrate shipped 2026-05-19 in `tools/ticket.py`); auto-defaults (ratify if `ratifiable_files` present, else action)
- `appetite: <int 1-10>` — bet's appetite score (ORG-089 §Decision.3); required at file for new bets (bet_ref present OR non-vault-ADR parent OR non-hygiene serves); medium-default (5) for legacy/hygiene per ORG-089 §Decision.4e applicability predicate
- `bet_ref: <ADR-id | T-NNNN>` — ADR/root-ticket this bet's appetite descends from (ORG-089); enables ADR-appetite → child-ticket propagation
- `substate: appetite-overrun` — enforcing state written in-place by grooming when appetite budget exceeds calibrated band (ORG-089 §Decision.4); refuses claim/delivery/resolve until re-decision command (`re-earn`/`drop`/`adjust-appetite`)
- `substate: dropped` — terminal-in-`tickets/done/` (ORG-089 §Landing bundle §Surface 4b, Ratified 2026-09-06 by the operator via TG 9047). **Distinct from resolved: DROPPED tickets do NOT satisfy dependencies (differ from resolved for ORG-013 cascade) and do NOT satisfy ORG-065 obligations.** The `_is_shipped(ticket)` predicate (build-tier scope per ORG-089 §Build plan) is the single source of truth: shipped iff `state: done` AND `substate != dropped`. Reached via `ticket.py drop --dropped-reason "<text>"` (direct) or `ticket.py drop` from `substate: appetite-overrun` (re-decision path). Reopen via `ticket.py reopen --re-earn-appetite <N> --rationale "<text>"` clears the substate + restarts the overrun clock. Distinct list/digest surface: "dropped this week" is calibration signal, not noise.
- `dropped_reason: <one-line>` — required when `substate: dropped`; disposition text.

Future substates (e.g., `claimed_by: <actor>` for explicit claim-actor; lock primitives per T-0463 follow-on) **MUST land as frontmatter amendments to this ADR**, not as new directories.

**The PWA renders substates via filter chips / visual grouping** (per ADR-052 / T-0367 in-flight). The directory state determines which view (inbox queue vs active board vs done archive); frontmatter substates determine within-view filtering + per-card rendering.

**The corollary: no new directories without an ADR amendment.** Future actors faced with a new substate question must propose the substate as a frontmatter field AND justify-via this ADR before any new directory lands. If the substate genuinely can't be expressed in frontmatter, that's the substrate-tier signal for the substrate-stacking-may-be-the-problem lesson — and triggers a substrate-level review (analogous to T-0453's retirement of blocked/), not a quiet new directory.

## Forces

- **P1 (architecture over policy)** — directory states are structural; frontmatter substates are structural; the boundary between them is the discipline this ADR locks in.
- **D10 (context-frugality)** — fewer directories means fewer cron-paths to walk + fewer code-paths in tools/ticket.py + tools/inbox_consumer.py + tools/ticket_grooming.py. Each new directory adds substrate-tier surface; each frontmatter substate adds bounded edit-tier surface.
- **`lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md`** — the discipline this ADR forward-applies. The retrospective form caught blocked/ retirement; this ADR is the forward-looking form preventing the next instance.
- **Specialist's noodle observation (T-0455 §1)** — 7 load-bearing substrate properties (operator-readable plain-text, git-audit, per-instance attribution, commit-time discipline, append-only ledger pre-cursor, cross-substrate consistency-for-free, plain-text discipline class) all survive at 3 directory states. Decomposition adds complexity without adding load-bearing properties.

## Alternatives considered

- **(A) Stay flexible — no taxonomy commitment.** Let new substates surface as directories OR frontmatter fields per per-case judgment. **Rejected:** the substrate-stacking-may-be-the-problem lesson's whole point is that ungrounded per-case judgment accreted six mechanisms on blocked/ before retirement. Explicit commitment is the structural fix.
- **(B) Compress further to 2 states (inbox + done; active absorbs into inbox with claimed-by frontmatter).** **Rejected:** active/ vs inbox/ encodes a real operator distinction (this is being worked-now vs queue-of-things-to-do); the substrate boundary is load-bearing for tools/inbox_consumer's pickup heuristic AND for HO/Specialist/Recovery's queue-walk discipline. 3 is the floor where the directory boundary still carries information.
- **(C) Expand to 4+ states with explicit ratify/, noodle/, etc. directories.** **Rejected:** explicitly the direction T-0453 / ORG-019 retired from. The PWA's filter-chip rendering (per ADR-052) achieves visual separation without substrate decomposition; intent disambiguation lives in frontmatter (T-0465).
- **(D) Selected — 3 directory states; substates as frontmatter.** Tightens the substrate-shape commitment; forward-applies the substrate-stacking lesson; preserves PWA UI flexibility.

## Consequences

### Substrate enforcement

`tools/ticket.py STATES` tuple stays `("inbox", "active", "done")` per ORG-019. Any future PR proposing a new state in this tuple gates on an ADR amendment to this ORG-021.

`tools/inbox_consumer.py`, `tools/ticket_grooming.py`, `tools/watch_runner.py`, `tools/ticket_state_sweep.py` all iterate the 3-state tuple per ORG-019. Same gate applies.

### Adding new substates

Future substate proposals (e.g., `claimed_by`, `intent`, lock-primitive fields) follow this path:

1. File a ticket scoping the substate (e.g., T-0465 for `intent`).
2. Draft an ADR amendment to ORG-021 OR a small "substate addition" ADR cross-referencing ORG-021, naming the new field + its semantics + the tool changes.
3. Ratify per ORG-015 allowlist (ADR is on the list).
4. Implement (frontmatter accept + tool changes + PWA rendering if user-facing).

### No effect on existing tickets

ORG-021 codifies the post-ORG-019 status quo. No existing tickets need migration; no frontmatter changes required. Backwards-compatible with all current tickets.

### Cross-references for in-flight design work

- **T-0463 (concurrent-claim primitive)** — already shipped (commit ad03aaf). Pre-claim origin/main state-check; if a `claimed_by` frontmatter field gets added (e.g., via T-0465 sibling), the pre-claim check can also validate it.
- **T-0465 (intent disambiguation)** — `intent: action|ratify|noodle` frontmatter; Part 1 substrate shipped 2026-05-19 (`tools/ticket.py`). Lives as a substate per ORG-021 (NOT new directories); named in the "current substates" list above.
- **T-0367 / ADR-052 (PWA UI: action-request vs ratify)** — already drafts the rendering side: section/chip-filter UI for substate-based grouping. ORG-021's PWA-rendering language tracks ADR-052's intent.

### Test coverage

ADR/SOP/docs-only row per `sop/standard/testing-depth-and-scope.md`: **Ratification REQUIRED** (this ticket). No code tests needed for the ADR itself; tests live with the implementing tickets when substates are added.

## Cross-references

- ORG-003 — ticket-system spec (this ADR specializes ORG-003's state machine)
- ORG-013 — cascade-unblock (amended by ORG-019; the `depends_on` substate's runtime semantics)
- ORG-015 — ratifiable-document-class allowlist (this ADR's ratification path)
- ORG-019 — blocked-state retirement (the substrate move this ADR locks-in-direction-of)
- T-0453 — the retirement cycle
- T-0455 — Specialist's design-noodle (proposed this ADR explicitly)
- T-0464 — this ADR's authoring ticket
- T-0465 — sibling intent-disambiguation (Part 1 substrate shipped 2026-05-19)
- `lessons/stacking-mechanisms-may-mean-the-substrate-is-the-problem.md` — the discipline this ADR forward-applies

---

**Status footer:** Drafted 2026-05-19. Ratified 2026-06-07 by the operator. Sets the substrate-shape commitment going forward; backwards-compatible with all current tickets.
