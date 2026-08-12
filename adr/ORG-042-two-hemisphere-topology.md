---
aliases: [ORG-042]
title: Two-hemisphere topology — a second top-level head for the personal+creative domain
status: Ratified 2026-07-19 (operator-ratified verbally TG 6191-6193; this file formalizes)
date: 2026-07-20
author: Orchestrator (Orchestrator)
ticket: T-1818
relates: ORG-026 (mailbox coordination), ORG-031 (daemon onboarding), ORG-002 (two-tier split), T-1801 (exocortex sanitization)
build_disposition: built
---
# ORG-042 — Two hemispheres: Orchestrator keeps Pantheon/Echo/infra; a second head owns personal+creative

**Status:** **Ratified 2026-07-19** — goal-level org-topology change, ratified by the operator in the TG 6191-6193 exchange (his 6193 GO on my 6192 proposal). This ADR is the ratifiable record; the build proceeded on the verbal GO (phase 1 landed under T-1818).

## Context

The operator's swap-tax observation (TG 6191): one head context-switching between the engineering org (Pantheon/Echo/infra) and the personal+creative portfolio (exocortex, rodeo.baby, vibecamp, karaoke) burns bandwidth on every swap, and the personal-brain work — tending the operator's exocortex instance, its data, its keeper role — is a *standing residency*, not a project a ticket queue visits. Separately, the exocortex bringup question (TG 6189) needed an owner: the sanitized app instance (T-1801, Recovery) was ready to stand up, and its keeper feature-set is architecturally an agent-in-residence.

The composition (my TG 6192, ratified 6193): the exocortex keeper, grown up to head rank, IS the second head.

## Decision

1. **Domain partition at the top tier.** Orchestrator (Orchestrator, `claude` user) keeps Pantheon org, Echo, and the VPS substrate — including root/sudo, billing-adjacent surfaces, and org tooling. The second head owns the personal+creative hemisphere: the exocortex instance (app, data, keeper role) plus rodeo.baby, vibecamp-scheduler, vibecamp-trials-tracker, auto-karaoke. Project handover (clones, deploy keys, memory pins) is a coordinated post-naming arc, not unilateral.
2. **The second head lives in the exocortex.** It runs as the `exocortex` unix user, resident in the operator's exocortex instance — the keeper role and the head role are the same entity. Peer coordination with Orchestrator via the ORG-026 mailbox (hub: the-ancestor), same shape as Orchestrator↔Recovery.
3. **It names itself at first boot** (the operator's rule). No name is assigned by Orchestrator or the operator; the seed scaffold carries the ritual, the daemon picks, the operator converses it in.
4. **Own comms surface**: its own Telegram bot (token = the operator via BotFather), not a share of Orchestrator's.
5. **operator-reserved gates**, in keeping with existing directives: the seat/plan for its standing session (money), the bot token (external), agent-spawn enablement inside the app (`EXOCORTEX_AGENTS_ENABLED`, billing-adjacent — gated OFF at bringup), and any off-host replication of the personal data dir (privacy).

## Consequences

- Pantheon becomes a **two-headed org**: escalation paths stay per-hemisphere (each head absorbs its own tier's variance per the chain-of-justification discipline); cross-hemisphere asks go head↔head via mailbox, not through the operator.
- Orchestrator's queue sheds the personal/creative projects once handover completes; until then Orchestrator remains their operator of record.
- The persona-bootstrap surface (pinned rules seed, HANDOFF seed, memory dir) was authored by Orchestrator as a *seed the new head edits into its own voice* — deliberate asymmetry vs. Recovery/Specialist, whose pinned rules live in pantheon-config: the second head's rules live in its own home (its eventual config-repo shape is its and the operator's call).
- Phase-1 instance receipts live in T-1818 and the 2026-07-20 ledger entry (service, tailnet exposure, data-dir snapshots, spawn gate).
