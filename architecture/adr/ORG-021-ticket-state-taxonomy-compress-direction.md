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
- `stale_after: <duration>` — grooming gate (ORG-003 §Front-matter field semantics; erratum T-4751: was cited as T-0235); used by `ticket_grooming.py` to flag tickets that have sat too long
- `intent: action|ratify|noodle` — target=operator queue disambiguation (T-0465, Part 1 substrate shipped 2026-05-19 in `tools/ticket.py`); auto-defaults (ratify if `ratifiable_files` present, else action)
- `appetite: <int 1-10>` — bet's appetite score (ORG-089 §Decision.3); required at file for new bets (bet_ref present OR non-vault-ADR parent OR non-hygiene serves); medium-default (5) for legacy/hygiene per ORG-089 §Decision.4e applicability predicate
- `bet_ref: <ADR-id | T-NNNN>` — ADR/root-ticket this bet's appetite descends from (ORG-089); enables ADR-appetite → child-ticket propagation
- `substate: appetite-overrun` — enforcing state written in-place by grooming when appetite budget exceeds calibrated band (ORG-089 §Decision.4); refuses claim/delivery/resolve until re-decision command (`re-earn`/`drop`/`adjust-appetite`)
- `substate: dropped` — terminal-in-`tickets/done/` (ORG-089 §Landing bundle §Surface 4b, Ratified 2026-09-06 by the operator via TG 9047). **Distinct from resolved: DROPPED tickets do NOT satisfy dependencies (differ from resolved for ORG-013 cascade) and do NOT satisfy ORG-065 obligations.** The `_is_shipped(ticket)` predicate (build-tier scope per ORG-089 §Build plan) is the single source of truth: shipped iff `state: done` AND `substate != dropped`. Reached via `ticket.py drop --dropped-reason "<text>"` (direct) or `ticket.py drop` from `substate: appetite-overrun` (re-decision path). Reopen via `ticket.py reopen --re-earn-appetite <N> --rationale "<text>"` clears the substate + restarts the overrun clock. Distinct list/digest surface: "dropped this week" is calibration signal, not noise.
- `dropped_reason: <one-line>` — required when `substate: dropped`; disposition text.

Future substates (e.g., `claimed_by: <actor>` for explicit claim-actor; lock primitives per T-0463 follow-on) **MUST land as frontmatter amendments to this ADR**, not as new directories.

**The PWA renders substates via filter chips / visual grouping** (per ADR-052 / T-0367 in-flight). The directory state determines which view (inbox queue vs active board vs done archive); frontmatter substates determine within-view filtering + per-card rendering.

**The corollary: no new directories without an ADR amendment.** Future actors faced with a new substate question must propose the substate as a frontmatter field AND justify-via this ADR before any new directory lands. If the substate genuinely can't be expressed in frontmatter, that's the substrate-tier signal for the substrate-stacking-may-be-the-problem lesson — and triggers a substrate-level review (analogous to T-0453's retirement of blocked/), not a quiet new directory.


## Frontmatter field registry (machine-read)

*(Added 2026-10-08, T-4751, operator-directed TG 11484/11485: "the ticket tool should directly reference the ADR for limitations".)*

The fenced YAML block below is the **single authoritative list of ticket frontmatter fields and their allowed values**. `tools/ticket.py` and every other ticket-frontmatter writer (`tools/inbox_consumer.py`, `tools/ticket_grooming.py`, the repair filer) read it from this section **at run time**. A write that adds or changes a field not listed here, or an enum field to a value not listed, is flagged, first as a WARN and later (T-4751 S2) refused. Values already on disk are never re-validated retroactively. `status: legacy` fields (retired by ORG-019 or hand-written before tooling) round-trip on read but may not be newly written. If this section cannot be read or parsed, writers **fail open with a loud WARN**: a broken edit here must never freeze ticketing.

The "current substates" prose above stays as the semantic description. This block is the machine-checkable list, and where they differ this block wins. Each entry names the field, its kind, its allowed values (enums only), when it is required, and the ratified ADR or implementing ticket that authorizes it. Field formats (timestamps, `T-NNNN` shapes, persona vocabulary) stay with the code and the cited authority; the registry does not restate them.

**Changing this registry.** Adding a field or value that codifies existing practice, or that a ratified ADR/ticket already authorizes, is a registry-only amendment: an HO merge, listed in the operator's digest (orchestrator ruling, T-4751; amended D3). Anything that changes the **directory-state rule** (exactly 3 states, no new folders), or a new value that changes ticket **workflow semantics** (as `dropped` vs resolved does for the ORG-013 cascade), still goes to the operator for ratification. A code change that writes a new field or value lands in the same PR as its registry entry; the drift test (T-4751) fails otherwise.

```yaml
registry_version: 1
fields:
  # required (ORG-003 core)
  - {field: id, kind: scalar, required: always, authority: "ORG-003 §Front-matter field semantics", status: active}
  - {field: created, kind: timestamp, required: always, authority: "ORG-003", status: active}
  - {field: requester, kind: scalar, required: always, authority: "ORG-003 (vocab: agent_role_map; T-0492, T-4499, T-4734)", status: active}
  - {field: target, kind: scalar, required: always, authority: "ORG-003; role:<R> form ORG-088 rev-2 / T-3761", status: active}
  - {field: state, kind: enum, values: [inbox, active, done], required: always, authority: "ORG-021 §Decision; ORG-019", status: active}
  # routing, lineage, purpose
  - {field: parent_ticket, kind: scalar, required: always, authority: "ORG-003; ORG-021", status: active}
  - {field: depends_on, kind: list, required: always, authority: "ORG-021; ORG-013 (T-3933); ORG-019", status: active}
  - {field: retargeted_at, kind: timestamp, required: "when retargeted", authority: "ORG-078 §5; T-3020", status: active}
  - {field: project_id, kind: scalar, required: never, authority: "ORG-054; T-3168", status: active}
  - {field: nearest_intent_ref, kind: scalar, required: never, authority: "ORG-054; T-3168", status: active}
  - {field: serves, kind: scalar, required: never, authority: "ORG-034; T-1103", status: active}
  - {field: labels, kind: list, required: never, authority: "ORG-003 (free vocabulary); ORG-021 (interactive-required)", status: active}
  - {field: tier_rank, kind: int, required: never, authority: "ORG-078 Amendment 1; T-5715 (the operator TG 12437/12447)", status: active}
  - {field: intent, kind: enum, values: [action, ratify, noodle], required: "when target == operator", authority: "ORG-021; T-0465", status: active}
  - {field: operator_intent, kind: enum, values: [action, ratify, noodle], required: never, authority: "T-5015", status: active}
  - {field: ratifiable_files, kind: list, required: "when intent == ratify", authority: "ORG-021; ORG-015", status: active}
  # gates
  - {field: not_before, kind: timestamp, required: never, authority: "ORG-021; ORG-019", status: active}
  - {field: not_before_anchor, kind: scalar, required: "when not_before is set (since T-2833)", authority: "T-2833; T-3103", status: active}
  - {field: decide_by, kind: timestamp, required: never, authority: "T-2209", status: active}
  - {field: stale_after, kind: scalar, required: never, authority: "ORG-003; ORG-021", status: active}
  - {field: retest_status, kind: enum, values: [pending, verified, overridden], required: never, authority: "ORG-038 S3*; T-2613", status: active}
  # typed edges (ORG-065)
  - {field: implements, kind: list, required: never, authority: "ORG-065 §3.2; T-2841", status: active}
  - {field: verifies, kind: list, required: never, authority: "ORG-065 §3.2; T-2841", status: active}
  - {field: supersedes, kind: list, required: never, authority: "ORG-065 §3.2; T-2841", status: active}
  - {field: informs, kind: list, required: never, authority: "ORG-065 §3.2; T-2841", status: active}
  - {field: ratifies, kind: list, required: never, authority: "ORG-065 Call 1 (the operator TG 6734); T-3476", status: active}
  - {field: receipts, kind: list, required: "when resolving with obligation edges", authority: "ORG-065 §3.3/§3.5; T-2842", status: active}
  - {field: orphan_reason, kind: scalar, required: "when filed with no lineage edge", authority: "T-3328", status: active}
  - {field: no_file_reason, kind: scalar, required: "when a target=operator ticket is filed/retargeted with --no-file", authority: "T-5014 (the operator TG 11815)", status: active}
  # appetite (ORG-089)
  - {field: appetite, kind: int, values: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], required: "when an applicable bet (ORG-089 §Decision.4e)", authority: "ORG-021; ORG-089 §Decision.3; T-3440", status: active}
  - {field: bet_ref, kind: scalar, required: never, authority: "ORG-021; ORG-089; T-3440", status: active}
  # substate family
  - {field: substate, kind: enum, values: [appetite-overrun, dropped, awaiting-merge], required: never, authority: "ORG-021 (appetite-overrun, dropped: ORG-089); awaiting-merge: T-4119", status: active}
  - {field: dropped_reason, kind: scalar, required: "when substate == dropped", authority: "ORG-021; T-3440", status: active}
  - {field: dropped_from_state, kind: enum, values: [inbox, active], required: "when substate == dropped", authority: "T-3440", status: active}
  - {field: awaiting_merge_ref, kind: scalar, required: "when substate == awaiting-merge", authority: "T-4119", status: active}
  - {field: security_gate, kind: enum, values: [pending, accepted, rejected], required: never, authority: "T-5678", status: active}
  - {field: security_rank, kind: enum, values: [primary, secondary], required: "when security_gate == accepted", authority: "T-5678", status: active}
  - {field: threat_foothold, kind: scalar, required: "when security_gate is set and threat_unclassified is not true", authority: "T-5678", status: active}
  - {field: threat_impact, kind: scalar, required: "when security_gate is set and threat_unclassified is not true", authority: "T-5678", status: active}
  - {field: threat_path, kind: scalar, required: "when security_gate is set and threat_unclassified is not true", authority: "T-5678", status: active}
  - {field: threat_justification, kind: scalar, required: never, authority: "T-5678", status: active}
  - {field: not_security_reason, kind: scalar, required: never, authority: "T-5678", status: active}
  - {field: threat_unclassified, kind: enum, values: ["true"], required: never, authority: "T-5678", status: active}
  - {field: security_filer, kind: scalar, required: "when threat_unclassified == true", authority: "T-5678", status: active}
  # consumer stamps (authority = implementing tickets, orchestrator ruling T-4751)
  - {field: auto_delivered, kind: bool, values: [true], required: never, authority: "T-0717; T-0971", status: active}
  - {field: auto_delivered_at, kind: timestamp, required: "when auto_delivered is set", authority: "T-0717; T-0971", status: active}
  - {field: delivered_to, kind: scalar, required: "when a role:<R> ticket is delivered or claimed", authority: "T-4695; T-4698; ORG-088 rev-2", status: active}
  - {field: auto_declined_by, kind: list, required: "when a seat was offered an inbox ticket and left it unclaimed past the stale threshold", authority: "T-5049; ORG-088", status: active}
  # runtime repair fields (ORG-076 / RFC-004 §3)
  - {field: fault_id, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: episode_id, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: domain_key, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: invariant_list_id, kind: scalar, required: never, authority: "RFC-004 §3; T-4707", status: active}
  - {field: invariant_list_digest, kind: scalar, required: never, authority: "RFC-004 §3; T-4707", status: active}
  - {field: subsystem, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: detector, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: verdict, kind: enum, values: [broken, unknown], required: "when repair ticket", authority: "RFC-004 §1, §3; T-4707", status: active}
  - {field: action, kind: scalar, required: never, authority: "RFC-004 §3; T-4707", status: active}
  - {field: evidence_deferred, kind: bool, values: [true, false], required: never, authority: "RFC-004 §6.16; T-4707", status: active}
  - {field: repair_route, kind: scalar, required: "when repair ticket", authority: "RFC-004 §3; T-4707", status: active}
  - {field: attempts_budget, kind: int, required: never, authority: "RFC-004 §3; T-4707", status: active}
  - {field: recovered_before_repair, kind: bool, values: [true, false], required: never, authority: "RFC-004; T-4707", status: active}
  - {field: evidence_snapshot, kind: block, required: never, authority: "RFC-004 §3, §6; T-4707", status: active}
  # legacy: round-trip on read, never newly written
  - {field: blocked_by, kind: scalar, required: never, authority: "ORG-019 (retired)", status: legacy}
  - {field: unblock_when, kind: scalar, required: never, authority: "ORG-019 (retired)", status: legacy}
  - {field: unblock_check, kind: scalar, required: never, authority: "ORG-019 (retired)", status: legacy}
  - {field: resolved, kind: timestamp, required: never, authority: "hand-written 2026-05 (T-0141), no tool writer", status: legacy}
  - {field: unblocked, kind: timestamp, required: never, authority: "hand-written 2026-05 (T-0145), no tool writer", status: legacy}
```

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

Future substate proposals (e.g., `claimed_by`, `intent`, lock-primitive fields) follow this path. A field or value that only codifies existing practice, or that a ratified ADR/ticket already authorizes, takes the lighter registry-only path in §Frontmatter field registry instead.

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
