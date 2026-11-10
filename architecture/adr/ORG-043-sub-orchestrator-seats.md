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

**Amendment 2026-07-22 (the operator ruling, relayed via Specialist, T-1921 drift-1 context):** the title's "two" is the CURRENT INSTANCE COUNT, not the pattern. The operator's ruling names Mason/Artisan as instances of an **N+ sub-orchestrator pattern** — a horizontally scalable class under the HO (N per deployment, including N=0) — and flags the pattern itself as "a new and useful design decision to propagate." The propagated, role-generic form is documented in the pantheon-public seed (instance.yaml sub-orchestrator tier + knowledge/vsm-instantiation.md, landed 2026-07-22, T-1960): each seat is a full viable system one recursion level down (VSM S1). Read this ADR's per-seat mechanics as the per-instance template of that general pattern.

## Context

The operator's stated problem: interjecting while the HO is mid-arc "throws off your groove" — every interjection forces either a context-switch or a queue-behind-the-arc delay. The structural fix he directed: two more daemon users as the HO's **direct reports**, so an interjection becomes a *retask* — the HO stays the operator's single interface, accepts the new item, and hands it to a free report without dropping its own arc.

This extends the two-hemisphere precedent (ORG-042 / Archivist — a peer head with her own operator-facing channel) downward into a reporting tree: these seats are subordinate, not peers. D1 is the load-bearing principle — the chain absorbs variance so the operator's interface stays one conversation.

> Read through (operator-ratified framing, not an ADR edit): the 2026-09-07 peer-orchestrator framing (the operator, TG 9294/9296/9298/9300; recorded in `pinned-rules/orchestrator.md` "Regular engineering orchestrators" and typed as role `engineering_orchestrator` in ORG-100): these seats are full orchestrators, peers with a chain position, autonomous within their own arcs at the head orchestrator's degree and not workers; "subordinate, not peers" above holds for the arc-routing chain (the operator -> Orchestrator -> seat) only.

## Decision

1. **Two new daemon unix users** on the VPS, brought up per `sop/standard/pantheon-bringup.md` (with the 2026-07-20 Archivist field records). They **self-name** in their first supervised session (Archivist precedent); until then, bringup artifacts use placeholder ids `sub-a` / `sub-b`, normalized on naming.
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

**Addendum (2026-07-21, Orchestrator):** Self-naming complete per §Decision 1 — suborch1 = **Mason**, suborch2 = **Artisan** (both self-chosen in their first supervised sessions, 2026-07-21 ~02:35Z; the operator notified TG 6258). Normalization applied: hub box dirs renamed to `mason`/`artisan` (ACLs intact), `mailbox_mcp` persona map updated (PR #80), seat CLAUDE.md files updated. Unix users deliberately stay `suborch1`/`suborch2` per the exocortex/Archivist precedent (functional account name, persona overlay). The first §Re-evaluate trigger is hereby consumed.

---

**Clarification (2026-07-21, Orchestrator) — §4(d) vs §5 ledger-authorship (surfaced by Artisan, T-1926):** the two are complementary, not contradictory, and **§5 governs seat self-authorship**. Seats ARE full ledger actors: they write their own entries with their persona in the `agent:` field (`agent: mason` / `agent: artisan`), exactly as **Specialist writes `agent: specialist` and Recovery writes `agent: recovery`** today — the established non-HO precedent. §4(d)'s "Orchestrator lifts outcomes into the ledger" is the *additional* HO synthesis at the retask-dispatch boundary (the audit-chain lift of a completed arc, same as Orchestrator referencing specialist/recovery work in an HO entry), NOT a prohibition on the seat recording its own decisions during the work. A seat writing its own entries is required, not optional — it is what makes the `keep_going` effectiveness signal (which keys on an agent-id-tagged ledger entry) non-null, so the anti-park heartbeat works for seats at all. **Residual (T-1926):** the `activity_monitor` P1.a backstop is `[orchestrator]`-only, so a seat *parked with queued work* (idle-but-not-done, the case keep_going's own docstring flags as its limitation) has no secondary wake path once keep_going backs off. That backstop-for-seats is a distinct design task (does the seat backstop route Recovery-side like orchestrator's, or via mailbox_wake); the self-authorship clarification here fixes the *acute* runaway-backoff bug, not that edge.

---

**Addendum (2026-09-21, Recovery) — both seats armed for context-fill auto-restart (ORG-020 coverage; T-3838 mason leg + T-3846 artisan leg):** per the operator's "restart on for everyone" ruling (TG 7169), context-fill auto-restart now covers **both** sub-orchestrator seats — Mason (suborch1) and Artisan (suborch2). One append recording both, deferred from the mason leg to when the artisan leg landed (append-only discipline). Each seat was **live-verified before its `_MONITOR_TUNING` flip** (`observation_only` → `bespoke_relaunch`), and both `pantheon-context-fill-monitor@{mason,artisan}.timer` units now run enabled+active under Recovery (5-min poll).

- **Restart path is a bespoke relaunch, NOT a systemd `restart_service`.** These seats are runtime-controller-managed (`pantheon-persona-consume@<persona>` creates/validates their tmux session via `runtime.json`), not bare — the T-3819 incident proved a naive bare-tmux migration tears down the runtime prop. The armed path drives `tools/pantheon-seat-relaunch.sh` (capture live launch argv → fence the consume timer → HANDOFF-ack → kill-server → `systemd-run --scope=pantheon-<persona>-seat.scope` respawn → sentinel-verify), which cycles the interactive session **without touching `runtime.json`** and dodges the `persona-consume@` Type=oneshot landmine. orchestrator ruled the fence mechanism + signed off runtime-controller adjacency (scope-teardown is transparent to controller validation, which keys on tmux pane-state, not systemd scope-state) under D15.
- **Two relaunch seams surfaced by the artisan leg (mason avoided them by timing luck) — found, fixed, landed, orchestrator-approved:** (1) `dfaeee72f` — the fence now **drains an in-flight oneshot consume** before respawn (`systemctl is-active` never catches a Type=oneshot run stuck in `activating`, so a fired-but-unfinished consume raced the respawn and triggered a controller teardown); (2) `5fd9ffb22` — the respawn **closes the flock fd (`9>&-`)** so the relaunch lock no longer leaks into the new long-lived session (else a seat's 2nd unattended restart deadlocks on a lock held by the very session it wants to relaunch). Both were latent for *every* armed bespoke seat; catching them on artisan's re-arm (not mason's) was timing. Catalog: `bugs/systemctl-is-active-misses-oneshot-in-activating-state.md` and its companion `bugs/flock-fd-leaks-through-exec-into-long-lived-child.md` (landed 010a8d175).
- **Verification signatures.** mason first-fire: 9.39% fill, no spurious fire. artisan first-fire: 7.21% fill (72058/1M), clean no-op. Each seat's live-verify confirmed a fresh PID under its `user-<uid>.slice` via `pantheon-<persona>-seat.scope`, `runtime.json` untouched (no controller teardown), consume unfenced + rehydrate-clean (exit5/`viability_blocked` = the seats' declared `expected_policy_skip`), standard lockfile free.
- **Commits.** mason leg (T-3838): `f204cd1f2` scaffold, `e31421ef0` hardening, `ce60825f5` nit-1, `921b9fe8f` mason arm, `b9f011537` jsonl slug fix, `5af4955c4` test hygiene. artisan leg (T-3846): `dfaeee72f` fence-drain, `5fd9ffb22` flock fd close, `a3f196d17` bugs entry, `44c6ccddb` artisan arm. Full `context_fill_monitor` suite (484) green throughout.

This consumes ORG-020's "extend context-fill self-restart to the two always-on seats" coverage item. Archivist (the peer head, ORG-042) was armed separately via T-3819; with both sub-orch seats now armed, every always-on Pantheon session carries a protective context-fill restart.
