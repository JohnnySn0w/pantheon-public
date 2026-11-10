---
aliases: [ORG-088]
id: ORG-088
title: "Role-based ticket routing — target: role:<R> with filter-at-pull-time delivery + existing stamp-mutex arbitration"
status: "Ratified 2026-09-06 by the operator via TG 9071 (rev-1). REV-2 Ratified 2026-09-17 by the operator via TG 10075 + TG 10082 (Call #1 'drop it yes' — DomainRegistry drop confirmed; 'so lets go' — architecture sign-off, option-1 parallel-build path). Original pivot direction TG 10059-10066. Mechanism simplified — the D-4 rewriter-executor (pre-delivery target:role:R → target:<persona> rebind, lowest-load selector, git-push arbitration state machine, classify-rebind-conflict, phantom-rebind fence) is REPLACED by filter-at-pull-time: role:R stays role-targeted, each seat's inbox_consumer filters eligible tickets by (target==my_persona OR target==role:R for R in functional_roles(my_persona)), and the EXISTING auto_delivered stamp mutex (git-push-atomic, already serializing personal-target delivery) arbitrates — first eligible seat to stamp wins, no new primitive. D-3 collapses to a single plane; D-6 simplifies. D-1/D-2/D-5/D-7/D-8 unchanged. D-5 (unknown-role typed refusal) + the Plane-A visibility surface SHIPPED in T-3761 (PR #440, main @610c72ee4) and are forward-compatible. Rev-1's rewriter-model build tranche (T-3757..T-3762) is retired; branches/ledger persist as historical audit. Consultant round-3 residuals R3-H1/R3-H2/R3-H3 (conflict-classification identity / single-writer arbitration / DomainRegistry+receipt reproducibility) are MOOT — the executor they concerned no longer exists. Drafted rev-1 2026-09-05 by Mason per T-3028; rev-2 drafted 2026-09-17 by Mason (the operator TG 10066 'let mason handle it'). Audit chain: notes/2026-09-06-org088-review-history.md (rev-1) + the retired-tranche ledger + dropped tickets T-3760/T-3782/T-3786/T-3787."
date: 2026-09-05
appetite: 8
build_disposition: needs-build
scoping_ticket: T-3028
---

# ORG-088 — Role-based ticket routing

**Status:** Ratified 2026-09-06 by the operator via TG 9071 (rev-1); **REV-2 Ratified 2026-09-17** by the operator via TG 10075 + TG 10082 ('drop it yes' → Call #1 DomainRegistry drop confirmed; 'so lets go' → architecture sign-off with option-1 parallel-build path). Simplifies the delivery mechanism per Rev-2 note below. **Date:** 2026-09-05 **Author:** Mason (sub-orchestrator-1) **HITL:** Autonomous **Related:** [[ORG-085]], [[ORG-027]], [[ORG-042]], [[ORG-043]], [[ORG-038]], [[ORG-059]], [[ORG-003]], [[ORG-013]], [[ORG-019]], [[ORG-065]], [[ORG-078]], [[T-3028]], [[T-1922]]

**Appetite:** 8/10

## Rev-2 (2026-09-17) — mechanism simplification per the operator TG 10059-10066

The operator's clarified intent (TG 10059-10061, verbatim): *"assign it to the role, and seat that are poked in the role should look at tickets assigned to them, and tickets assigned to their role"* — and (TG 10061) *"there's no race condition there."* Directive TG 10064: *"halt and shape it correctly this time."*

**What this rev changes.** Rev-1's D-4 introduced a **pre-delivery rewriter-executor**: an HO-tier stage that rebinds `target: role:R → target: <persona>` using a herd-free lowest-load selector, with git-push arbitration, a `classify_rebind_conflict` cross-clone reconciliation path, and a durable fence guarding the window where an unpushed rebind could phantom. The operator's read retires that entire machine. The **same concurrency the auto_delivered stamp mutex already handles for personal-target tickets** handles role-target tickets — applied at the same delivery-time point, with a wider pull-time filter. No rewriter, no selector, no arbitration state machine, no fence.

- **D-3** collapses from two planes (passive visibility + active single-assignee assignment) to a **single plane**: visibility and delivery are the same pull-time membership filter; exactly-once is the existing stamp mutex.
- **D-4** is **replaced** in full (see D-4 below): pull-time filter + existing stamp-mutex arbitration; the domain/delegation eligibility engine and the DomainRegistry-narrowing-at-assignment are **dropped** — narrowing is now (a) role granularity (`role:orchestrator` vs `role:sub_orchestrator` vs `role:orchestrator`, all declared at the L3 source) and (b) volitional retarget (D-7).
- **D-6** simplifies: no separate assignment stage between visibility and delivery.
- **D-1, D-2, D-5, D-7, D-8 unchanged** as ratified in rev-1. D-5 (unknown-role typed refusal) and the Plane-A visibility surface (list/ready/prime) already SHIPPED in T-3761 (PR #440) and are forward-compatible with this rev.
- **Consultant round-3 residuals R3-H1/R3-H2/R3-H3 are MOOT** — they concerned the executor's conflict-classification identity, single-writer arbitration, and DomainRegistry+receipt reproducibility; none of those components exist in rev-2.

*Ratifies-via:* mechanism-simplification under the operator's authoritative architecture intent (TG 10059-10066); preserves the rev-1 concept (route by role, `role:` namespace, L3 membership authority) and all back-references.

## Context

`tools/inbox_consumer.py:974` — `if target != agent and target != resolve_role(agent): continue`. The role-match compares the persona's single ~1:1 audit role-id, so `target: orchestrator` reaches only orchestrator — archivist (also HO-role, [[ORG-042]]) has audit id `archivist` and is invisible. No shared-functional-envelope routing exists, though Pantheon runs multiple personas per functional role ([[ORG-042]], [[ORG-043]], [[ORG-027]]).

The operator's directive (TG 8157): *"tasks should just sort to them architecturally."*

[[ORG-085]] specifies the resolve_role_target behavior and **delegates mechanics to this ADR** (verbatim): candidates = declared personas whose role_ids contains role_id; empty → retain queued + no_eligible_persona receipt; one → atomic claim + receipt; multiple → require deterministic selector on authority-sourced inputs, else retain + ambiguous_persona_for_role refusal. ORG-085's constraint: *"T-3028 owns ticket target syntax, WIP and availability rules, selector policy, atomic claim mechanics… requires deterministic, idempotent, receipt-bearing, fail-closed behavior. Naming a stronger Role never launders authority."* (Rev-2 note: ORG-085's *delegation* of mechanics to this ADR stands; the specific "single deterministic selector / atomic claim" mechanics it anticipated are satisfied differently in rev-2 — the delivery-time stamp mutex is the atomic, idempotent, receipt-bearing serializer, and "which member" is decided by first-to-stamp rather than a computed selector. See D-4.)

## Appetite

**Score: 8/10** (value 8 / strategic 9 / reversibility 8, average → 8.33). Rubric: `knowledge/episodic/2026-09-05-deference-heuristic-proposal/appetite-scale.md`.

Role-routing is a core mechanism that unlocks HO-distribution scaling — the operator directly asked for it, ORG-085 explicitly delegates the mechanics here, and coexists cleanly with volitional persona-targeting. Value is real (archivist + Mason + Artisan become reachable via functional-role work-pool); strategic alignment is direct (dogfoods ORG-085 + ORG-027 planes); reversibility is genuine (D-8 additive, `role:` prefix is a new value not a schema change). **Rev-2 reduces the build surface substantially** — the executor stage, selector, arbitration state machine, fence, and DomainRegistry are removed; the remaining build is a pull-time filter reusing the existing delivery+stamp path.

Explicit ceiling: the ADR body names the design SURFACE (role: namespace + the pull-time membership filter + visibility surface + unknown-role refusal + volitional retarget + additive migration). It does NOT specify method signatures, per-consumer line numbers, or build-time parameter values — those belong in T-3028's follow-on build tickets. **Not-in-scope:** L3 PersonaDefinition schema code; the exact filter call-site; test-file names or fixture shapes. Any of those growing into the ADR body triggers halt + re-scope (same lesson as ORG-089 rev-2 → rev-3, the operator TG 9040; and this rev-2 itself, TG 10064).

## Riskiest assumption

**That the auto_delivered stamp mutex which serializes personal-target delivery across the fleet also correctly serializes role-target delivery — i.e. when two eligible seats both pull the same `role:R` ticket, the existing git-push-atomic stamp makes exactly one deliver and the other skip, with no double-delivery and no lost ticket.**

This is the operator's asserted invariant (TG 10061, *"there's no race condition there"*): role-target tickets reuse the *same* delivery-time primitive personal-target tickets already use, only reached through a wider pull-time filter. Cheap test / mitigation: the stamp-mutex path already exists and is exercised by every personal-target delivery; the build's falsifier is a two-seats-pull-same-role-ticket concurrency test asserting exactly-once stamp + no lost ticket, plus a stamp-race test under coincident push. If the stamp mutex turns out NOT to serialize a role-target ticket that two seats reached via different personas (e.g. the stamp keys on persona rather than ticket identity), REBOUND to fix the stamp's identity basis — but this is a small, localized concern versus rev-1's whole arbitration machine. (Rev-1's riskiest assumption — the L3 freshness/digest contract carrying arbitration reproducibility — is largely retired: no executor depends on a reproducible DomainRegistry digest. The L3 source is still read for `functional_roles(persona)` at filter time, but only for membership, not for a reproducible arbitration input.)

## Decision

### D-1 — `target: role:<R>` (explicit `role:` namespace) — UNCHANGED

`role:<R>` is one value of the existing single `target:` field; the `role:` prefix disambiguates it from a persona name and the legacy audit-role-id match, so no existing value changes meaning. *Justifies-via:* P1 + [[ORG-085]] + [[ORG-003]].

### D-2 — Membership owned by L3 PersonaDefinition; instance.yaml only projects — UNCHANGED

Functional-role membership is owned **solely by the L3 PersonaDefinition** per [[ORG-085]] §Conceptual serializable signatures (the PersonaBinding paragraph: "The binding does not own functional-role membership") and §Field ownership and authority. The binding layer (`instance.yaml`) **must not author `role_ids`**; it may carry a read-only, digest-bound projection, verified against the L3 digest at load and refused on mismatch (no second authority). `agent_role_map.py` provides a reader over the L3 source (`functional_roles(persona)` / `role_members(role)` / `known_role_ids()` — the last shipped in T-3761). The `orchestrator` role is the truthful [[ORG-027]] orchestrator tier (Orchestrator/Specialist/Recovery) plus [[ORG-042]] Archivist, [[ORG-043]] Mason/Artisan, and codex/Consultant ([[ORG-038]]). The roster is resolved from the L3 declarations, not enumerated as authority here. *Justifies-via:* [[ORG-085]] + [[ORG-027]] + [[T-1922]] + P4 + P6.

### D-3 — Single plane: visibility and delivery are one pull-time membership filter — REV-2 (was two planes)

A `role:R` ticket is routed by a single mechanism: at pull time each seat's `inbox_consumer` treats a ticket as addressed to it when `target == <my_persona>` **OR** `target == role:R` for some `R ∈ functional_roles(<my_persona>)`. The same filter drives both **visibility** (`ticket.py list`/`ready`/`prime` show it to members — shipped in T-3761) and **delivery** (the per-persona delivery loop considers it). Exactly-once across the fleet is the **existing** `auto_delivered` stamp mutex — the git-push-atomic stamp that already prevents double-delivery of personal-target tickets. There is no separate active-assignment plane and no `target:` rewrite: a `role:R` target stays `role:R` for its whole lifetime; the first eligible seat to deliver stamps `auto_delivered` and wins, and every other seat sees the stamp on its next pull and skips. *Justifies-via:* [[ORG-085]] (exactly-one delivery) + P1 + D11.

### D-4 — Filter-at-pull-time + existing stamp-mutex arbitration (REPLACES rev-1's resolve_role_target executor)

- **No executor, no rewriter.** Rev-1's HO-tier assignment stage, the `(effective_load ASC, persona_id ASC)` herd-free selector, the git-push arbitration state machine, `classify_rebind_conflict`, and the phantom-rebind fence are all **removed**. Nothing rebinds `role:R` to a persona.
- **Membership filter (every seat, at pull time).** A seat is eligible for a `role:R` ticket iff `R ∈ functional_roles(<my_persona>)` (read from the L3 source, D-2). No domain/delegation/availability engine gates it — any member of the role may take a role ticket. **Narrowing is expressed two ways, both already available:** (a) *role granularity* — HO-only work targets `role:orchestrator` (only orchestrator/archivist are members), broadly-distributable work targets `role:orchestrator` (all orchestrator-tier), sub-orchestrator work targets `role:sub_orchestrator`; the role you pick IS the audience. (b) *volitional retarget* (D-7) — HO may name a specific persona. The rev-1 DomainRegistry / `permitted_domains` / `work_domain` / sub-orch-`delegation` eligibility machinery is **dropped**; ORG-043's "sub-orchs act on HO-retasked arcs" is honored by targeting the narrower role or by explicit retarget, not by an automated per-ticket domain filter.
- **Arbitration = the existing stamp mutex.** When an eligible seat delivers a `role:R` ticket it stamps `auto_delivered` via the same transactional, git-push-atomic commit used for personal tickets; the stamp keys on **ticket identity**, so a second seat that pulled the same ticket via a different role loses the push race (or sees the stamp on rebase) and skips. Idempotent and receipt-bearing by construction (the stamp IS the receipt). First-eligible-to-stamp wins; there is deliberately **no load-balancing or fairness selector** (the operator TG 10061 — the stamp mutex is sufficient; load-fairness was a rev-1 concern and is dropped).
- **Fail-closed edges (unchanged in spirit):** an unverifiable L3 membership source ⇒ the read surfaces degrade-graceful (hide role tickets, keep the listing alive) and the write surfaces refuse (D-5) — as shipped in T-3761.

*Justifies-via:* [[ORG-085]] (delivery-time exactly-once) + [[ORG-027]]/[[ORG-043]] (role granularity carries the tier distinction) + P4 (the stamp is the receipt) + P5 + D11.

### D-5 — Unknown `role:<R>` is a typed refusal, fail-closed — UNCHANGED (SHIPPED in T-3761)

`ticket.py file`/`retarget` + `ticket_lint.py` **reject** unknown `role:<R>` with a typed error (never accept-with-WARN). Landed in T-3761 (PR #440). *Justifies-via:* [[ORG-085]] + [[ORG-003]] + P4.

### D-6 — Pooled fallback + ORG-078 order — REV-2 (simplified)

An unassigned role ticket is simply an **unstamped** role ticket; it stays visible and deliverable on every pull until an eligible seat stamps it. There is no separate assignment stage. Delivery still respects [[ORG-078]] priority order over the frontier — a `role:R` P1 is delivered ahead of a P3 — but the frontier is the ordinary delivery frontier, not a distinct "eligible-for-assignment" set. *Justifies-via:* [[ORG-078]] + P1.

### D-7 — Volitional distribution preserved — UNCHANGED (SUPERSEDED 2026-10-19: seat targeting removed, see §Amendment 2026-10-19)

HO may `retarget --to <persona>` to name an owner or `--to role:<R>` to re-pool. In rev-2 this is also the primary *narrowing* mechanism alongside role granularity. *Justifies-via:* P1 + P6 + [[ORG-003]].

### D-8 — Additive migration — UNCHANGED

`role:<R>` is a new *value* of `target:`, no schema migration; the legacy audit-role-id path is untouched. *Justifies-via:* [[ORG-003]] + P7.

## Consequences

**positive:**
- Archivist, Mason, Artisan, Consultant all become reachable via functional-role work-pool (HO-distribution scales beyond orchestrator-only).
- **No new arbitration primitive** — role delivery reuses the auto_delivered stamp mutex that already serializes personal-target delivery; far smaller build + attack surface than rev-1.
- No rewriter means **no phantom-rebind window**, so no fence, no classify-rebind-conflict, no single-writer HO-tier executor.
- Any orchestrator head being down no longer parks role tickets — every eligible seat pulls them directly.
- Additive: no existing `target:` value changes meaning; legacy path untouched.

**negative:**
- **No load-balancing:** first-eligible-to-stamp wins, so a busy seat can grab a role ticket ahead of an idle one (accepted per the operator TG 10061; load-fairness was a rev-1 concern, now out of scope).
- **No automated domain narrowing:** any role member may take any ticket for that role; keeping HO-tier work off a sub-orch relies on picking the right role granularity or on volitional retarget, not on an enforced per-ticket filter.
- The exactly-once guarantee now rests entirely on the stamp mutex keying on ticket identity across seats reaching the ticket via different roles (see §Riskiest assumption).

**deferred:**
- L3 PersonaDefinition schema code + the exact filter call-site in inbox_consumer — build ticket.
- The two-seats-pull-same-role-ticket concurrency falsifier — build ticket.
- Method signatures, per-consumer line numbers — build tickets, not the ADR body.

**retired (rev-1 → rev-2):**
- resolve_role_target executor stage; `(effective_load ASC, persona_id ASC)` selector; git-push arbitration state machine; classify_rebind_conflict; phantom-rebind fence; DomainRegistry + `permitted_domains`/`work_domain`/`delegation` eligibility. Consultant residuals R3-H1/R3-H2/R3-H3 are moot. Rev-1 build tranche T-3757..T-3762 retired (T-3760/T-3782/T-3786/T-3787 dropped; branches/ledger persist as historical audit).

**open questions:**
- The L3 role-membership roster is the operator's (persona_roles.yaml seed, T-3757). Role *granularity* now carries what the DomainRegistry carried in rev-1 (orchestrator vs sub_orchestrator vs orchestrator, etc.) — if finer audiences are needed, they are new roles at the L3 source, a data edit, mechanism-agnostic.

**relates to tensions:** the exactly-once invariant depends on the stamp mutex's identity basis (§Riskiest assumption); if the stamp keys on persona rather than ticket, a role ticket reached via two personas could double-deliver — the build's concurrency falsifier is the guard.

## Review + build history

- Rev-1 ORG-041 review chain (Mason-authored, Consultant-reviewed): rounds 1-2 REBOUND findings resolved in rev-3-of-rev-1; round 3 CAP-EXHAUSTED with residuals R3-H1/R3-H2/R3-H3. Full audit in `notes/2026-09-06-org088-review-history.md`.
- Rev-1 build tranche (T-3757 L3 source → T-3758 DomainRegistry → T-3759 executor → T-3760 inbox wiring → T-3761 CLI → T-3762 finale): T-3757/T-3758/T-3759 merged; **T-3761 (CLI/lint + Plane-A visibility) merged (PR #440, main @610c72ee4) and is forward-compatible**; T-3760 (executor wiring) + its round-3/4/5 fix tickets (T-3776/T-3779/T-3781/T-3782/T-3786/T-3787) retired by rev-2 (dropped or resolved; branches/ledger persist).
- **Rev-2 build (needs-build):** a single pull-time membership filter in inbox_consumer's delivery path (`target==me OR role∈functional_roles(me)`), reusing the existing auto_delivered stamp mutex for exactly-once + the ORG-078 order, + the two-seats-concurrency falsifier + a migration/back-compat check. The T-3761 visibility surface + D-5 refusal already ship. Tranche + falsifier list per the rev-2 scoping (T-3028 follow-on, to be filed by HO).

## Amendment 2026-09-27 — ORG-100 role vocabulary (T-4003)

Owed by [[ORG-100]] §Pending downstream ADR amendments (operator-ratified 2026-09-19). Appended; the ratified body above is unchanged.

- **Valid `role:<R>` values** are the role ids in `persona_roles.yaml` (the L3 source, D-2):
  - `head_engineering_orchestrator` (orchestrator)
  - `head_research_orchestrator` (specialist)
  - `head_system_orchestrator` (recovery)
  - `head_memory_orchestrator` (archivist)
  - `head_auditor` (codex)
  - `engineering_orchestrator` (mason, artisan)
- **There is no universal `orchestrator` role** (ORG-100 §5). The open-questions examples above (`orchestrator`, `sub_orchestrator`, `orchestrator`) are retired names. Read them as `head_engineering_orchestrator`, `engineering_orchestrator`, and no equivalent. D-5's unknown-role refusal applies to all three.
- **`engineering_orchestrator` is the one multi-member role**, so `role:engineering_orchestrator` is the D-1 pooling case (mason or artisan pulls it). The bare id used as a persona `target:` is not a pool and must be refused, not reverse-resolved to one seat (T-4044).

## Amendment 2026-10-19 — seat targeting removed (T-5239)

The operator, TG 12076/12077 (filed as T-5239), restated TG 12121/12122/12123: "we want the tool to not allow naming a specific seat", "Change ticket.py so it refuses a seat name as a target", "amend any adr that needs it". Appended; the ratified body above is unchanged except for D-7's heading marker.

- **D-7 is superseded.** A ticket `target:` is a `role:<R>` pool or `operator`, nothing else. `ticket.py file` and `ticket.py retarget` refuse a persona (seat) name with a typed refusal that names the role(s) to use instead. `ticket_lint` flags a persona target on any live ticket. The `requester:` field is provenance, not routing, and still records the persona.
- **Narrowing is by role granularity alone.** When only one seat should take the work, target that seat's single-member head role (for example `role:head_engineering_orchestrator` for orchestrator, `role:head_system_orchestrator` for recovery). There is no per-seat assignment path, so "that's X's queue" is not a state a ticket can be in.
- **Membership is the L3 source's, including implied roles.** `role:engineering_orchestrator` resolves to artisan, mason and orchestrator (the head role implies the base role). The 2026-09-27 amendment's "(mason, artisan)" listing predates that and should be read with orchestrator included.
- **Consequences correction.** "Additive: no existing `target:` value changes meaning" no longer holds for persona values: they are now refused on write and flagged by lint. Existing persona-targeted live tickets are re-pooled to a role. Done and archived tickets keep their historical `target:` values (audit chain, ORG-001).
- **Justifies-via:** The operator's direct instruction (above); P1 (the tool enforces the routing rule structurally instead of relying on filer discipline: two seat-targeted filings slipped through on 2026-10-19 under the old D-7); D1 (no per-seat queues for the operator to reason about).
