# Lesson — the inbox auto-consumer races an orchestrator's in-flight implementer

**Surfaced:** 2026-06-24, Echo Phase 2 arc (T-1142). Filed by Orchestrator (HO).

## What happened

The project-orchestrator (`echo-phase2-orch`) filed its Phase-2 build sub-tickets (T-1296, T-1297) in `inbox/` as plain-actionable, `target=orchestrator`, per ticket-out-each-phase. It then dispatched its own implementer to build **T-1296** on `agent/T-1296`. While that implementer was mid-build, the **inbox auto-consumer fired on T-1296** and prompted the HO to *also* claim + build it — a double-dispatch onto the same files/branch. Caught before any second build started.

## Root cause

The inbox auto-consumer fires on any unclaimed `target=orchestrator` inbox ticket. It **cannot distinguish** a ticket that's genuinely awaiting pickup from one an orchestrator's implementer is *already building*. A plain-actionable sub-ticket of an active orchestrator arc looks identical to fresh work. The orchestrator drives its arc's sub-tickets itself; the auto-consumer doesn't know that.

## The fix (convention)

**Build sub-tickets of an active orchestrator arc must carry a suppression gate at file-time:**
- **`depends_on: [T-prev]`** for sequenced ones — the honest gate; also cascade-unblocks (ORG-013) when the predecessor resolves. Use this whenever the sub-ticket genuinely sequences after another.
- **`interactive-required`** for judgment/data-gated ones the auto-consumer should never fire on.

The orchestrator (not the auto-consumer) then claims each to `active/` when its implementer picks it up. The gate is what tells the auto-consumer "this is orchestrator-driven, hands off."

## Resolve/merge serialization

The HO holds the `active/` claim for the in-flight sub-ticket (1-active-per-agent WIP cap serializes naturally). On the orchestrator's reviewer-APPROVE report: HO pushes `agent/<t>`, merges the PR, resolves the ticket → cascade-unblocks the next, and claims *that* to `active/` in the same motion before the auto-consumer can fire — keeping the chain serialized + race-free.

## Promotion

Structural fix **landed** (per [[structural-enforcement-retires-the-prose-rule]] — prose convention in a brief is not reliable): the at-file-time suppression-gate convention is now encoded in both targets — [`sop/standard/team-orchestrator-dispatch-checklist.md`](../sop/standard/team-orchestrator-dispatch-checklist.md) §1 ("Arc sub-ticket gating briefed", cites this lesson) and the `pantheon-dispatch` skill (`SKILL.md` filing-flow discipline: block at file-time when `depends_on` is non-empty). HO no longer relies on per-arc verbal briefing; the convention is in the dispatch checklist + skill.
