---
aliases: [ORG-088]
id: ORG-088
title: "Role-based ticket routing — target: role:<R> with fail-closed single-assignee resolution"
status: "Ratified 2026-09-06 by the operator via TG 9071. Drafted 2026-09-05 by Metis (sub-orchestrator-1) per T-3028; ORG-041 rev-3 clean; shape-rewrite to canonical template 2026-09-06 by Orchestrator (D2 HO-mechanical, substance unchanged; audit chain preserved in notes/2026-09-06-org088-review-history.md). Cairn round-3 residuals (conflict-classification identity R3-H1 / single-writer arbitration R3-H2 / DomainRegistry+receipt reproducibility R3-H3 + roster/DomainRegistry open question) accepted as ratifiable tradeoffs — carried into build tickets under T-3028's follow-on tranche, not blocking ratify."
date: 2026-09-05
appetite: 8
build_disposition: needs-build
scoping_ticket: T-3028
---

# ORG-088 — Role-based ticket routing

**Status:** Ratified 2026-09-06 by the operator via TG 9071 **Date:** 2026-09-05 **Author:** Metis (sub-orchestrator-1) **HITL:** Autonomous **Related:** [[ORG-085]], [[ORG-027]], [[ORG-042]], [[ORG-043]], [[ORG-038]], [[ORG-059]], [[ORG-003]], [[ORG-013]], [[ORG-019]], [[ORG-065]], [[ORG-078]], [[T-3028]], [[T-1922]]

**Appetite:** 8/10

## Context

`tools/inbox_consumer.py:974` — `if target != agent and target != resolve_role(agent): continue`. The role-match compares the persona's single ~1:1 audit role-id, so `target: orchestrator` reaches only orchestrator — mnemosyne (also HO-role, [[ORG-042]]) has audit id `mnemosyne` and is invisible. No shared-functional-envelope routing exists, though Pantheon runs multiple personas per functional role ([[ORG-042]], [[ORG-043]], [[ORG-027]]).

The operator's directive (TG 8157): *"tasks should just sort to them architecturally."*

[[ORG-085]] specifies the resolve_role_target behavior and **delegates mechanics to this ADR** (verbatim): candidates = declared personas whose role_ids contains role_id; empty → retain queued + no_eligible_persona receipt; one → atomic claim + receipt; multiple → require deterministic selector on authority-sourced inputs, else retain + ambiguous_persona_for_role refusal. ORG-085's constraint: *"T-3028 owns ticket target syntax, WIP and availability rules, selector policy, atomic claim mechanics… requires deterministic, idempotent, receipt-bearing, fail-closed behavior. Naming a stronger Role never launders authority."*

## Appetite

**Score: 8/10** (value 8 / strategic 9 / reversibility 8, average → 8.33). Rubric: `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`.

Role-routing is a core mechanism that unlocks HO-distribution scaling — the operator directly asked for it, ORG-085 explicitly delegates the mechanics here, and coexists cleanly with volitional persona-targeting. Value is real (mnemosyne + Metis + Atlas become reachable via functional-role work-pool); strategic alignment is direct (dogfoods ORG-085 + ORG-027 planes); reversibility is genuine (D-8 additive, `role:` prefix is a new value not a schema change).

Explicit ceiling: the ADR body names the design SURFACE (role: namespace + planes + eligibility + selector + receipts + refusal categories). It does NOT specify method signatures, per-consumer line numbers, full state machine specs, or build-time parameter values — those belong in T-3028's follow-on build tickets. **Not-in-scope:** L3 PersonaDefinition schema code; DomainRegistry storage format; the exact effective_load metric computation; classify_rebind_conflict wire-level logic; test-file names or their fixture shapes. Any of those growing into the ADR body triggers halt + re-scope (same lesson as ORG-089 rev-2 → rev-3, the operator TG 9040).

## Riskiest assumption

**The L3 PersonaDefinition source of authority for role_ids exists, is machine-readable at consumer time, and can carry the freshness + digest guarantees ORG-085 requires — without a separate registry.**

Cheap test / mitigation: the build's first phase (per T-3028 §Build) is exactly this — file a landing that reads role_ids from PersonaDefinition + digest-binds instance.yaml's projection. If the L3 source can't carry the freshness/digest contract cleanly (missing revision field, no digest primitive, no atomic read), the design regresses to "role-registry as a separate authority" — a documented alternative in ORG-085 that this ADR explicitly rules out. Re-evaluate at build-phase-1 landing: if `PersonaRoleMembership` can't be resolved from the L3 source with the freshness contract, REBOUND to ORG-085 for a registry-primitive amendment before completing D-2's guarantee.

## Decision

### D-1 — `target: role:<R>` (explicit `role:` namespace)

`role:<R>` is one value of the existing single `target:` field; the `role:` prefix disambiguates it from a persona name and the legacy audit-role-id match, so no existing value changes meaning. *Justifies-via:* P1 + [[ORG-085]] + [[ORG-003]].

### D-2 — Membership owned by L3 PersonaDefinition; instance.yaml only projects

Functional-role membership is owned **solely by the L3 PersonaDefinition** per [[ORG-085]] §188-191/246-260. The binding layer (`instance.yaml`, which authors role→persona/account/harness *bindings*) **must not author `role_ids`**; it may carry a read-only, digest-bound projection for convenience, verified against the L3 digest at load and refused on mismatch (no second authority). `agent_role_map.py` gains a reader over the L3 source, not a literal map. The `orchestrator` role is the truthful [[ORG-027]] orchestrator tier (Orchestrator/Specialist/Recovery) plus [[ORG-042]] Mnemosyne, [[ORG-043]] Metis/Atlas, and codex/Cairn (standing orchestrator per codex's AGENTS.md, the assurance/review domain — [[ORG-038]]). The roster is resolved from the L3 declarations, not enumerated as authority here; narrowing to who may take a given ticket is the D-4 domain filter, never bare membership. *Justifies-via:* [[ORG-085]] + [[ORG-027]] + [[T-1922]] + P4 + P6.

### D-3 — Two planes: passive visibility vs active single-assignee delivery

- **Plane A — passive visibility (read-only):** `ticket.py list`/`ready` show a `role:R` ticket to members as pool-visibility; no delivery, stamp, or mutex.
- **Plane B — active assignment (exactly-once, before delivery):** the assignment executor (D-4) rebinds `target: role:R` → `target: <persona>` **before** the per-persona delivery loop — i.e. before the check_dispatch_locked / list_eligible / deliver / stamp_auto_delivered sequence in inbox_consumer, and before the dispatch mutex. Post-assignment the target is a persona and the existing single-target path runs unchanged; no `role:R` target ever reaches delivery/stamp/mutex, so fan-out is structurally impossible.

*Justifies-via:* [[ORG-085]] (exactly-one binding) + P1 + D11.

### D-4 — resolve_role_target: HO-tier executor, authority eligibility, herd-free selector, atomic claim

- **Executor:** the assignment stage runs in the HO-tier consumer (orchestrator or mnemosyne — the ratified distribution hub, [[ORG-027]]/[[ORG-043]]), as a distinct stage before the per-persona delivery loop. It is the single writer for role-pool assignment; the two heads are a convergent pair (each computes the same assignment from shared authoritative state; residual cross-head concurrency is resolved by the atomic rebind below). If no head is live, role tickets stay in Plane A (visible, unassigned) — fail-closed.
- **Eligibility:** role-member AND `work_domain ∈ permitted_domains(persona)` (typed DomainRegistry) AND (sub-orch ⇒ `delegation` proves HO-retask per [[ORG-043]]) AND available AND delivery-reachable. Typed refusal on absent/unknown `work_domain` or missing-required `delegation`. No authority inferred from labels, requester, or prose.
- **Selector (herd-free):** order eligible candidates by the total order **`(effective_load ASC, persona_id ASC)`**, where `effective_load` counts active WIP + assigned-but-inactive inbox + in-flight + reservations-made-this-pass. The executor processes pool tickets in a stable order, incrementing the winner's running load after each assignment, so N coincident role tickets distribute instead of all binding the lexicographically-first zero-WIP persona. Fail-closed (`ambiguous_persona_for_role`) if an authoritative input is unavailable.
- **Atomic claim + cross-clone reconciliation:** rebind via the transactional ticket commit; the assignment receipt goes in the transition log. On push non-ff conflict for this ticket, the classify algorithm distinguishes `already_assigned` (yield cleanly) from rebase-retry (conflict elsewhere) from typed auth/network/corruption failure (clean checkout + alert). Idempotent: re-resolving an assigned ticket is a no-op.

*Justifies-via:* [[ORG-085]] + [[ORG-027]]/[[ORG-043]] (domain/retask authority) + [[ORG-059]] (delegation under native authority) + P4 (receipts) + P5 + D11.

### D-5 — Unknown `role:<R>` is a typed refusal, fail-closed

`ticket.py file`/`retarget` + `ticket_lint.py` **reject** unknown `role:<R>` with a typed error (never accept-with-WARN). *Justifies-via:* [[ORG-085]] + [[ORG-003]] + P4.

### D-6 — Pooled fallback + ORG-078 order

Unassigned role tickets stay on Plane A, retried each executor pass. Fixed order: **visibility (A) → [[ORG-078]] priority-sort → assignment (B) over the sorted eligible frontier** — a `role:R` P1 is assigned ahead of a persona P3. *Justifies-via:* [[ORG-078]] + P1.

### D-7 — Volitional distribution preserved

HO may `retarget --to <persona>` to name an owner or `--to role:<R>` to re-pool. *Justifies-via:* P1 + P6 + [[ORG-003]].

### D-8 — Additive migration

`role:<R>` is a new *value* of `target:`, no schema migration; the legacy audit-role-id path is untouched. *Justifies-via:* [[ORG-003]] + P7.

## Consequences

**positive:**
- Mnemosyne, Metis, Atlas, Cairn all become reachable via functional-role work-pool (HO-distribution scales beyond orchestrator-only).
- Fan-out is structurally impossible (D-3 assign-before-deliver invariant), not a discipline that must be remembered at every call site.
- Herd-free selector prevents N coincident role tickets all binding the same lexicographically-first zero-WIP persona.
- Additive: no existing `target:` value changes meaning; legacy path untouched.

**negative:**
- HO-tier executor becomes a single-writer for role-pool assignment; if both heads are down, role tickets sit in Plane A until a head returns (accepted fail-closed).
- Requires typed `work_domain` on every role: ticket — new authoring discipline.
- Cross-clone reconciliation adds a classify-rebind-conflict path in the ticket lifecycle.

**deferred:**
- L3 PersonaDefinition schema code + storage format (build ticket).
- DomainRegistry storage format + freshness_rule wire encoding (build ticket).
- classify_rebind_conflict wire-level logic + test fixtures (build ticket).
- Method signatures, per-consumer line numbers, and full state-machine specs — all belong in T-3028's follow-on build tickets, not the ADR body.

**open questions:**
- The L3 role-membership roster and the DomainRegistry contents (work_domain + delegation vocabularies) are the operator's. Proposed: `orchestrator` = {orchestrator, specialist, recovery, mnemosyne, metis, atlas, codex} narrowed per-ticket by the DomainRegistry (sub-orchs eligible only for HO-retasked arcs; specialist/recovery/codex only for research/recovery/assurance domains); optional `orchestrator` and `sub-orchestrator` narrower roles. All live at the L3 source + DomainRegistry; reshaping is a data edit, mechanism is membership-agnostic. Not blocking the design.

**relates to tensions:** L3 authority for role_ids depends on PersonaDefinition freshness contract (§Riskiest assumption); if the L3 source can't carry the guarantee, this design regresses to a separate registry authority (ORG-085 documents but rules out).

## Review + build history

- ORG-041 review chain (Metis-authored, Cairn-reviewed): rounds 1-2 REBOUND findings F1-F5 (round 1) + R2-F1-F4 (round 2) resolved in rev 3. Full round-by-round finding→amendment table + typed-contracts pseudo-code (deferred to build tickets) preserved for audit in `notes/2026-09-06-org088-review-history.md`.
- Cairn round 3/3 CAP-EXHAUSTED with 3 HIGH residuals (conflict-classification identity R3-H1; single-writer arbitration R3-H2; DomainRegistry+receipt reproducibility R3-H3) + roster/DomainRegistry open question. Dispositions live in T-3028 transition log; HO lean at ratify-time was (b) ratify-with-dispositions per the escalation memo.
- Build tranche follows ratify per T-3028 §Build (needs-build, labels `adr-088` + `build`): L3 PersonaRoleMembership source + digest-bound projection → typed ticket fields + DomainRegistry + eligible() predicate → HO-tier assignment executor stage → inbox_consumer Plane-A + strict pre-delivery assignment → ticket.py list/ready/retarget + lint refusal → falsifier tests (herd-free, two-heads-concurrent, crash-after-assign, post-assign-delivery-fail, domain-ineligibility, selector determinism, no-eligible/ambiguous/unreachable, receipt idempotency, starvation guard, unknown-role refusal, additive-migration, ORG-078 compose).
