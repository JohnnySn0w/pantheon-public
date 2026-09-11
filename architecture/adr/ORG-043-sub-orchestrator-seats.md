---
aliases: [ORG-043]
id: ORG-043
title: "Sub-orchestrator seats: two daemon reports under the HO"
status: Ratified — direction ratified by the operator 2026-07-20 (TG 6245, verbatim in T-1892); design details authored autonomously per msg-3733 (below-goal, adopted under P/Ds + ledger)
date: 2026-07-20
build_disposition: built
---

# ORG-043 — Sub-orchestrator seats: two daemon reports under the HO

**Status:** Direction ratified by the operator 2026-07-20 (Telegram 6245, verbatim in T-1892); design details authored autonomously per msg-3733 (below-goal-level, adopted under P/Ds + ledger). **Date:** 2026-07-20 **Author:** Orchestrator (HO) **Ticket:** T-1893 (parent arc T-1892).

**Amendment 2026-07-22 (the operator ruling, relayed via Specialist, T-1921 drift-1 context):** the title's "two" is the CURRENT INSTANCE COUNT, not the pattern. The operator's ruling names Metis/Atlas as instances of an **N+ sub-orchestrator pattern** — a horizontally scalable class under the HO (N per deployment, including N=0) — and flags the pattern itself as "a new and useful design decision to propagate." The propagated, role-generic form is documented in the pantheon-public seed (instance.yaml sub-orchestrator tier + knowledge/vsm-instantiation.md, landed 2026-07-22, T-1960): each seat is a full viable system one recursion level down (VSM S1). Read this ADR's per-seat mechanics as the per-instance template of that general pattern.

## Context

The operator's stated problem: interjecting while the HO is mid-arc "throws off your groove" — every interjection forces either a context-switch or a queue-behind-the-arc delay. The structural fix he directed: two more daemon users as the HO's **direct reports**, so an interjection becomes a *retask* — the HO stays the operator's single interface, accepts the new item, and hands it to a free report without dropping its own arc.

This extends the two-hemisphere precedent (ORG-042 / Mnemosyne — a peer head with her own operator-facing channel) downward into a reporting tree: these seats are subordinate, not peers. D1 is the load-bearing principle — the chain absorbs variance so the operator's interface stays one conversation.

## Decision

1. **Two new daemon unix users** on the VPS, brought up per `sop/standard/pantheon-bringup.md` (with the 2026-07-20 Mnemosyne field records). They **self-name** in their first supervised session (Mnemosyne precedent); until then, bringup artifacts use placeholder ids `sub-a` / `sub-b`, normalized on naming.
2. **Reporting line: the operator → Orchestrator → report.** The reports have **no Telegram bots** and no operator-facing channel. All upward comms route through Orchestrator (mailbox); the emergency bypass is the existing Recovery relay (recovery umbrella) or the mailbox severity flag once T-1641 Phase 2 lands — a report never pings the operator directly.
3. **Comms + task substrate: existing machinery, no new protocols.** ORG-026 mailbox seats (hub boxes with trusted-N ACLs incl. both grant directions + default ACLs per the T-1824/T-1870 lessons), ticket lanes (`target: <persona>`), keep-going/mailbox-wake poke loops (T-1827 machinery) for daemon liveness.
4. **Retask protocol.** When the operator hands Orchestrator work while an arc is held: (a) Orchestrator files a ticket capturing the ask verbatim + context; (b) dispatches it to a free report via mailbox with a **dispatch brief carrying the full chain-of-justification discipline** (D7 — intent-first, DECISIONS-with-justifies-via, bubble-up criteria, recursive propagation); (c) the report drives it in its own session — its own subagent teams, its own project worktrees — and resolves the ticket with the normal evidence trail; (d) Orchestrator lifts outcomes into the ledger and surfaces to the operator per D1 digest discipline. Reports bubble up to Orchestrator on the standard 5 criteria only; Orchestrator remains the only tier that bubbles to the operator.
5. **Persona citizenship.** The reports are full ticket/ledger actors (persona-name `agent:` fields, like specialist/recovery). Same worker-guard posture as other non-HO seats for shared records: push/PR/merge on shared repos follows each repo's existing single-push-actor conventions; project repos assigned to a report get per-repo deploy keys per the standing convention.
6. **Accounts + usage.** Each seat gets its own Claude Code login (one the operator OAuth grant per instance, batched — agent-oauth-relogin SOP flow). All seats draw the shared Max pool; `usage_monitor` (+ T-1875 extensions when landed) is the watch surface. Any usage-driven throttling decision is the operator's (financial-decisions-reserved).

## Consequences

- The operator's interjections stop costing HO-arc integrity; the queue drains in parallel instead of serially behind the HO's current arc.
- The HO's dispatch discipline (briefs, chain-of-justification, review gates) now has a second enforcement site: retask briefs to reports must carry it explicitly, same as subagent briefs — the D7 propagation rule applies verbatim.
- Two more daemons share the usage pool and the maintenance surface (creds cliffs like T-1878, relogin cycles, unit hygiene). The bringup tickets bake in OnFailure alert wiring (T-1868/T-1869 pattern) from day one.
- Emergency-path asymmetry (reports have no direct the operator channel) is deliberate: it preserves the single-interface property that motivated the change. Re-evaluate if a report-held arc ever stalls on Orchestrator-unavailability for >1 cycle — that failure shape would argue for a Recovery-style fallback channel.

## Re-evaluate when

Self-naming completes (normalize placeholders); OR the first supervised retask retro (T-1896) contradicts the protocol shape; OR usage-pool pressure forces a scheduling policy; OR an Orchestrator-unavailability stall exercises the emergency path.

---

**Addendum (2026-07-21, Orchestrator):** Self-naming complete per §Decision 1 — suborch1 = **Metis**, suborch2 = **Atlas** (both self-chosen in their first supervised sessions, 2026-07-21 ~02:35Z; the operator notified TG 6258). Normalization applied: hub box dirs renamed to `metis`/`atlas` (ACLs intact), `mailbox_mcp` persona map updated (PR #80), seat CLAUDE.md files updated. Unix users deliberately stay `suborch1`/`suborch2` per the exocortex/Mnemosyne precedent (functional account name, persona overlay). The first §Re-evaluate trigger is hereby consumed.

---

**Clarification (2026-07-21, Orchestrator) — §4(d) vs §5 ledger-authorship (surfaced by Atlas, T-1926):** the two are complementary, not contradictory, and **§5 governs seat self-authorship**. Seats ARE full ledger actors: they write their own entries with their persona in the `agent:` field (`agent: metis` / `agent: atlas`), exactly as **Specialist writes `agent: specialist` and Recovery writes `agent: recovery`** today — the established non-HO precedent. §4(d)'s "Orchestrator lifts outcomes into the ledger" is the *additional* HO synthesis at the retask-dispatch boundary (the audit-chain lift of a completed arc, same as Orchestrator referencing specialist/recovery work in an HO entry), NOT a prohibition on the seat recording its own decisions during the work. A seat writing its own entries is required, not optional — it is what makes the `keep_going` effectiveness signal (which keys on an agent-id-tagged ledger entry) non-null, so the anti-park heartbeat works for seats at all. **Residual (T-1926):** the `activity_monitor` P1.a backstop is `[orchestrator]`-only, so a seat *parked with queued work* (idle-but-not-done, the case keep_going's own docstring flags as its limitation) has no secondary wake path once keep_going backs off. That backstop-for-seats is a distinct design task (does the seat backstop route Recovery-side like orchestrator's, or via mailbox_wake); the self-authorship clarification here fixes the *acute* runaway-backoff bug, not that edge.
