---
aliases: [ORG-067]
id: ORG-067
title: "Deprecate the-ancestor persona: post-office as the drop-in hub, Specialist absorbs the curation walks"
status: SUPERSEDED 2026-09-04 by [[ORG-087]] — the-ancestor deprecation (ratified form). ORG-067 was the first-cut DRAFT (2026-08-09) with a narrower framing that mis-attributed the grooming/curation walks; the honest inventory + rulings live in ORG-087 (Ratified 2026-09-04 by the operator via Telegram 8965). Kept in corpus per superseded-conceptions-remain-visible convention.
date: 2026-08-09
---

# ORG-067 — Deprecate the-ancestor; post-office is the drop-in

**Status: DRAFT — awaiting the operator ratify.** Drafted 2026-08-09 by Orchestrator (HO) per
The operator's ticket-walk direction (TG 7522 "i want to deprecate the ancestor. Specialist
can do the walks. shoot me a design doc" + TG 7524 "the vsm map should be
relatively unaffected, the post-office is the replacement drop-in"). T-2734. This
record scopes the design; **nothing executes until the operator ratifies** — org
identity is an S5 call (ORG-038).

## Arc-intent

The org sheds a **special-case persona** — `the-ancestor` — by folding its two
functions into things that already exist: its comms/hub role into a purpose-named
`post-office` user, and its would-be curation role into **Specialist**. The result is a
less-special-cased VSM with one fewer bespoke identity to reason about, install,
and carry — serving the grand goal's substrate-simplicity (ORG-053: fewer organs
with clearer contracts). Per the lifecycle contract, this is the **remove** leg of
`the-ancestor` and a **modify** (rename) of the hub organ — both reversible,
staged, and monitored below.

## The decision (one line)

`the-ancestor` is deprecated as a distinct persona/user. Its **mailbox-hub role**
is taken over by a purpose-named **`post-office`** user (a *drop-in* — same VSM
substrate slot, renamed, not remapped). Its **curation/grooming role** — the one
[[ORG-043]]'s rebalance-trigger contemplated promoting it into — goes to **Specialist**
instead. `the-ancestor` the user is scrubbed and retired.

## Context — why remove rather than promote

- `the-ancestor` today is a **restart-gated mailbox hub with no standing session**
  — it owns `/home/the-ancestor/mailbox`, through which all inter-agent comms flow,
  but it is not an agent; it has no variety of its own. It is a bespoke identity
  that exists mostly as a filesystem owner + a name in the VSM map.
- **T-1968** raised the mapping doc's own rebalance-trigger: *"if the ancestor role
  ever gains active curation duties (grooming memory, arbitrating contested
  records), revisit — that would make it an S1 unit."* The question was: promote
  `the-ancestor` into a curation S1 unit by giving it the weekly grooming walks?
  **the operator's answer (TG 7522) was the opposite:** don't grow a new bespoke unit —
  remove this one and put the walks where a real seat with real variety already
  sits (Specialist, S4, which already runs the knowledge sweep).
- **T-2693** already plans to write `the-ancestor` out at the infra level and
  replace it with a `post-office` user (post-T-2683 installer). This ADR **absorbs
  and sequences T-2693 as its infra half**, rather than duplicating it.

So deprecation is not a teardown of function — it is a **rename of the hub organ**
(→ post-office) plus a **relocation of the curation role** (→ Specialist). Nothing the
org depends on is lost; two functions move to better-named homes.

## What deprecation means — three strands

### Strand A — Mailbox hub: `the-ancestor` → `post-office` (drop-in; = T-2693)

The load-bearing half. `post-office` is a **drop-in replacement** for the hub: same
role, same 2770/`pantheon-mailbox` group semantics, new owner + path
(`/home/post-office/mailbox`). This is exactly T-2693's phased, reversible,
operator-gated cutover — it is not re-designed here, only claimed as Strand A:

1. Create the `post-office` user + hub root + group (T-2683 installer step-1).
2. **Drain in-flight mail** — quiesce senders / confirm all `new/` consumed so
   nothing is lost mid-move.
3. Migrate `/home/the-ancestor/mailbox` → `/home/post-office/mailbox` (copy tree,
   re-own, preserve perms/group).
4. Repoint all references (`instance.yaml` hub path/owner, `tools/mailbox.py`
   constant, the systemd unit `PANTHEON_MAILBOX_HUB` env, group memberships).
5. Verify a full mailbox round-trip on the `post-office` hub (send+consume each
   direction) **before** retiring anything.
6. Retire `the-ancestor` user after a soak. **Rollback:** repoint back to
   `/home/the-ancestor/mailbox` (kept intact until soak passes).

### Strand B — Curation walks → Specialist

The weekly grooming/curation walks and where each sits today:

| Walk | Today | Under ORG-067 |
|---|---|---|
| Knowledge-sweep triage | **Specialist** (`knowledge_sweep.py`, timer) | Specialist (unchanged) |
| Policy-outflow sync | **Specialist** (`policy_outflow_sync.py`) | Specialist (unchanged) |
| Watch digest | **Specialist** (`watch_digest.py`) | Specialist (unchanged) |
| Ticket grooming | HO manual | **→ Specialist** (candidate) |
| Spine / ledger INDEX | HO manual | **→ Specialist** (candidate) |
| Memory-pin sweep | HO manual (ORG-012 §5) | **→ Specialist** (candidate; cross-user coverage was already the open T-0872 gap) |
| Ledger grooming | HO / mixed | **→ Specialist** (candidate) |

Specialist already **is** the curation-shaped seat (S4, environment + knowledge). The
move is: the HO-manual walks that T-1968 would have handed to a new `the-ancestor`
unit go to Specialist instead. **Open for the build phase:** which of the four
"candidate" walks move wholesale vs stay HO (some — e.g. ledger grooming tied to
the audit chain — may be better kept at HO/S3); that granularity is a build-time
call, not this design's to fix.

### Strand C — VSM map: rename, do not remap

Per the operator (TG 7524): **the VSM map is relatively unaffected.** `post-office` is a
drop-in, so it occupies `the-ancestor`'s existing **substrate** slot — the node is
**renamed** `the-ancestor` → `post-office`, not restructured. Specialist's S4 position
is unchanged; it simply carries the curation duties it was already shaped for. So
the ORG-038 / ORG-053 mapping reconciliation is a **doc rename**, not a
re-derivation. This ADR deliberately does **not** overwork the VSM section — the
org model does not change shape; a bespoke substrate owner is renamed to a
purpose-named one, and a role the org already had moves to a seat it already has.

## Phased sequence (design-first; execution ratify-gated)

- **Phase 0 — ratify** (this doc). S5/the operator.
- **Phase 1 — Specialist absorbs the walks** (Strand B): decide the candidate-walk
  granularity, move them to Specialist, verify each runs green on its schedule. Reversible
  (walks return to HO).
- **Phase 2 — hub → post-office** (Strand A / T-2693): the phased operator-gated
  cutover above. Reversible (repoint back until soak passes).
- **Phase 3 — scrub + retire**: grep-driven scrub of `the-ancestor` references
  (ORG-045/026, `agent_role_map`, `fleet_health`, docs), rename the VSM substrate
  node (Strand C), retire the `the-ancestor` user after soak.

Phases 1 and 2 are independent and can run in either order; Phase 3 follows both.

## Reversibility / rollback

- Strand A: T-2693's own rollback — repoint to the intact `the-ancestor` mailbox
  until the post-office soak passes. High-care, operator-gated window.
- Strand B: walks are timers/scripts — moving one to Specialist and back is a config
  change, no data loss.
- Strand C: a doc/reference rename + a user retire; the retire is the only
  hard-to-undo step and comes **last, after both soaks pass**.

## Lifecycle contract (ORG-053 §6)

- **remove** `the-ancestor`: the user + its bespoke role, after its functions have
  demonstrably moved (round-trip verified, walks green on Specialist).
- **modify** the hub organ: rename/relocate to `post-office`, contract unchanged.
- **monitor**: `fleet_health` + the mailbox round-trip check must pass on
  `post-office` before the retire; the moved walks must show green runs on Specialist's
  schedule before their `the-ancestor`-candidate status is closed.

## What stays vs goes

- **Goes:** the `the-ancestor` user/persona as a distinct identity; its name across
  the codebase/docs; its VSM node name.
- **Stays (renamed/relocated):** the mailbox-hub *function* (→ `post-office`); the
  curation *function* (→ Specialist). No capability is lost.

## Open questions for the operator (ratify inputs)

1. **Strand-B granularity:** move all four candidate walks (ticket grooming, spine
   index, memory-pin sweep, ledger grooming) to Specialist, or keep ledger grooming at
   HO/S3 (audit-chain proximity)? (HO lean: move the first three; keep ledger
   grooming HO-adjacent — but your call.)
2. **Sequencing:** Phase 1 (walks) and Phase 2 (hub) are independent — any
   preference on order, or run them as capacity allows?
3. **Naming:** `post-office` as the hub user name (per T-2693) — confirm, or another
   purpose-name?

On ratify, this becomes the in-force ORG-067 and the phases spin into build tickets
(Strand A = T-2693 executes; Strand B/C = new build tickets).
