# Pantheon ADRs

Architectural Decision Records for the org tier. Project ADRs live in their respective project repos; this directory is for decisions that span the hub or amend Pantheon's principles.

## Numbering

`ORG-NNN`. Default starting at `ORG-001`. Separate from any project's ADR ranges. Project ADRs live in their project's repo (per the operator msg_1199 + msg_1200 + msg_1205):
- `echo-prismatic/adr/` — 1–199 reserved for operator-authored vault, 200+ for agent-authored.
- `pantheon-pwa/adr/` — 1–49 reserved for operator-authored vault (if/when), 50+ for agent-authored (established 2026-05-13 via T-0302; first entry ADR-050).
- Future project repos: file a sibling ADR establishing their own range; check existing ranges to avoid collision.

Pantheon (this directory) uses `ORG-NNN` exclusively; no overlap with project numeric ranges.

## HITL field (mandatory)

Every ADR carries one of:
- **Authored** — human-written by the operator. Implicit on principle amendments he writes directly.
- **Ratified** — agent-proposed, operator-accepted. Status line carries `Ratified: YYYY-MM-DD by the operator` and the channel where ratification happened (Telegram, terminal, GitHub PR).
- **Autonomous** — agent-authored, unratified. Default state for orchestrator-initiated ADRs awaiting the operator's review.

## When to write one

- Choosing between reasonable approaches with rationale that matters
- Defining or changing an interface (between tiers, with workers, with project orchestrators)
- Amending or extending a principle in `rulebook/principles.md`
- Resolving (or explicitly preserving — see P6) a tension
- Any decision that crosses the autonomy bar (D3)

Do NOT write ADRs for: trivial refactors, styling, pure scaffolding, or things `BACKLOG.md` already tracks as work items.

## Format

Match the operator's vault-ADR style (see `echo-prismatic/adr/TEMPLATE.md` for the canonical shape, lifted from the operator's authored ADRs 001–011). Inline-header style, not a folder-per-ADR.

## Index

| ADR | Title | Status | Date |
|---|---|---|---|
<!-- AUTOGEN-START spine-index -->
| [ORG-001-ledger-format.md](ORG-001-ledger-format.md) | Decision ledger format and storage | Ratified 2026-04-28 | 2026-04-28 |
| [ORG-002-principles-directives-split.md](ORG-002-principles-directives-split.md) | Two-tier framework: Principles + Directives | Ratified 2026-04-28 | 2026-04-28 |
| [ORG-003-ticket-system.md](ORG-003-ticket-system.md) | Ticket system: format, lifecycle, and grooming | Ratified 2026-05-02 | 2026-05-02 |
| [ORG-004-mcp-transport-bridge.md](ORG-004-mcp-transport-bridge.md) | MCP transport bridge for fleet messaging (Telegram now; web UI later) | Phase 1 closed-as-abandoned; Phase 2 superseded by ORG-006; bridge daemon + repo DECOMMISSIONED 2026-06-04 (T-0767) | 2026-05-02 |
| [ORG-005-mcp-channel-reliability-diagnosis-first.md](ORG-005-mcp-channel-reliability-diagnosis-first.md) | MCP channel reliability: diagnosis-first fix path | Ratified 2026-05-04 | 2026-05-04 |
| [ORG-006-kino-pwa.md](ORG-006-kino-pwa.md) | kino PWA — Pantheon multi-agent web UI (post-bridge pivot) | Ratified | 2026-05-05 |
| [ORG-007-kino-claude-wire-protocol.md](ORG-007-kino-claude-wire-protocol.md) | kino ↔ claude wire protocol — stream-json long-running process per agent | Ratified | 2026-05-07 |
| [ORG-009-kino-pwa-ticket-api-contract.md](ORG-009-kino-pwa-ticket-api-contract.md) | kino PWA ticket-API contract | Ratified 2026-05-10 | 2026-05-10 |
| [ORG-010-three-tier-policies-extension.md](ORG-010-three-tier-policies-extension.md) | Three-tier framework: Principles + Directives + Policies | Ratified | 2026-05-10 |
| [ORG-011-stays-working-under-silence.md](ORG-011-stays-working-under-silence.md) | Structural enforcement for HO-stays-working-under-silence | Ratified 2026-05-11 | 2026-05-11 |
| [ORG-012-information-inflow-bubble-up-methodology.md](ORG-012-information-inflow-bubble-up-methodology.md) | Information-inflow bubble-up methodology | Ratified 2026-05-14 | 2026-05-14 |
| [ORG-013-relational-triggers-on-ticket-resolve.md](ORG-013-relational-triggers-on-ticket-resolve.md) | Relational triggers on ticket resolve (auto-unblock cascade + blocked-revisit digest) | Ratified 2026-06-07 by the operator (authored 2026-05-14) | 2026-05-14 |
| [ORG-014-request-spawn-primitive.md](ORG-014-request-spawn-primitive.md) | RequestSpawn primitive (Shape 2 / b33 followup) | Ratified 2026-05-17 | 2026-05-17 |
| [ORG-015-ratifiable-document-class-allowlist.md](ORG-015-ratifiable-document-class-allowlist.md) | Ratifiable document class allow-list | Ratified 2026-05-17 by the operator (T-0400 verdict 17:04Z). | 2026-05-17 |
| [ORG-016-multi-model-orchestration-shape-a-pilot.md](ORG-016-multi-model-orchestration-shape-a-pilot.md) | Multi-model orchestration: Shape A pilot (Claude-primary + Grok/GPT as MCP-wrapped tools) | Ratified 2026-05-17 | 2026-05-17 |
| [ORG-017-knowledge-tree-unification.md](ORG-017-knowledge-tree-unification.md) | Unify `watch/` + `research/` under `knowledge/` (Option C) | Ratified 2026-05-17 | 2026-05-17 |
| [ORG-018-codex-cli-mcp-wrap-pilot.md](ORG-018-codex-cli-mcp-wrap-pilot.md) | Codex CLI as MCP tool source: pilot (Option 2 from msg_1640+1641) | Ratified 2026-05-17 | 2026-05-17 |
| [ORG-019-blocked-state-retirement.md](ORG-019-blocked-state-retirement.md) | Retire `tickets/blocked/` as a state; blocks expressed via `not_before` or `depends_on` only | Ratified 2026-05-19 | 2026-05-18 |
| [ORG-020-self-triggered-restart-for-all-pantheon-agents.md](ORG-020-self-triggered-restart-for-all-pantheon-agents.md) | Self-triggered restart mechanism for all Pantheon agents | Ratified 2026-05-20 | 2026-05-20 |
| [ORG-021-ticket-state-taxonomy-compress-direction.md](ORG-021-ticket-state-taxonomy-compress-direction.md) | Pantheon ticket-state taxonomy: 3 directory states; substates-as-frontmatter | Ratified 2026-06-07 by the operator (authored 2026-05-19) | 2026-05-19 |
| [ORG-022-ratifiable-pairing-enforcement-at-file-tier.md](ORG-022-ratifiable-pairing-enforcement-at-file-tier.md) | Ratification label ↔ ratifiable_files pairing enforcement at ticket-file-tier | Ratified 2026-05-19 | 2026-05-19 |
| [ORG-023-watch-runner-kanban-port-decision.md](ORG-023-watch-runner-kanban-port-decision.md) | watch_runner kanban-primitive port decision — reject full port, conditional minimal circuit-breaker | Superseded 2026-05-29 by ORG-025 (verdict carried; framing superseded) | — |
| [ORG-024-orchestration-philosophy-project-is-the-eval.md](ORG-024-orchestration-philosophy-project-is-the-eval.md) | ORG-024 — Orchestration philosophy: the project is the eval | Ratified 2026-05-29 | 2026-05-28 |
| [ORG-025-specialist-orchestration-model.md](ORG-025-specialist-orchestration-model.md) | Specialist orchestration model — interactive-session watch-orchestration with researcher-subagent dispatch (retires headless claude -p watch billing) | Ratified 2026-05-29 by the operator (T-0696); trigger mechanism folded in per msg_2683 | — |
| [ORG-026-inter-agent-message-passing-substrate.md](ORG-026-inter-agent-message-passing-substrate.md) | Inter-agent message-passing substrate — daemonless file-mailbox (Maildir) + systemd --user .path wake | Ratified 2026-06-05 by the operator (amendment — §4 resolves to LIFT design/DIY, not adopt-dependency; attack-surface minimization) | — |
| [ORG-027-orchestrator-tier-with-domains-reclass.md](ORG-027-orchestrator-tier-with-domains-reclass.md) | Reclass the always-on agents to orchestrator-tier-with-domains (retire the worker-vs-orchestrator split for persistent agents) | Ratified 2026-06-05 by the operator via T-0789. Drafted 2026-06-04 by Recovery (T-0785, spawned from noodle T-0764). | 2026-06-04 |
| [ORG-028-portable-config-topology.md](ORG-028-portable-config-topology.md) | Portable config topology — per-harness config repos + role-keyed layout + per-instance manifest (no submodules) | Ratified 2026-06-06 by the operator via T-0845. Drafted 2026-06-05 by Recovery (claude-fallback) per the operator directive ("draft it", Telegram 2026-06-05). Open questions resolved in ORG-030 (awaiting ratification). | 2026-06-05 |
| [ORG-029-pantheon-distribution-model.md](ORG-029-pantheon-distribution-model.md) | Pantheon distribution model — upstream-reference scaffolding + fork-microcosm history + path-disjoint reconciliation | Ratified 2026-06-05 by the operator (authored by Orchestrator/HO) | — |
| [ORG-030-portable-config-migration-plan.md](ORG-030-portable-config-migration-plan.md) | Portable config — open-questions resolution + migration plan (resolves ORG-028's 4 deferred questions) | RATIFIED 2026-06-07 by the operator (T-0881). Resolves the open questions ORG-028 deferred; execution stays gated per ORG-028's "no forcing function today" (phase tickets T-0913/0914/0915 parked pending un-gate). | 2026-06-06 |
| [ORG-031-harness-abstraction-and-daemon-onboarding.md](ORG-031-harness-abstraction-and-daemon-onboarding.md) | Harness-abstraction adapter + generalized daemon-onboarding (codex = first non-claude case) | RATIFIED 2026-06-09 by the operator (via T-0897, resolved 2026-06-09T03:12:58Z). Drafted 2026-06-06 by Orchestrator (HO). Ratifies T-0712 deliverable (2): the adapter interface, the Option-(b) policy decision, and the harness-agnostic onboarding procedure. Codifies decisions; execution of the onboarder generalization is sub-ticketed (T-0898) and reversibility-classified. | 2026-06-06 |
| [ORG-032-revenue-project-orchestration-shape.md](ORG-032-revenue-project-orchestration-shape.md) | Money-engine orchestration shape — status-quo now, dedicated agent at actuation-un-defer (the ORG-031 onboarder retires the cost objection) | Adopted 2026-06-11 by Orchestrator (HO) — autonomous under P/D justification per the operator msg 3733 (principles-as-ratification; ratify-asks below goal-level retired). Resolves T-0377. Decision is trigger-based: Option D/A now, committed to Option B at the named trigger. NOTE: the STATE-2 trigger (un-defer of revenue-project actuation) remains goal-level and the operator's. | 2026-06-08 |
| [ORG-033-multi-instance-architecture.md](ORG-033-multi-instance-architecture.md) | Multi-instance architecture — Pantheon's three-Claude-Code-instance discipline (consolidating) | Ratified 2026-06-10 by the operator (Telegram 3100) | 2026-06-10 |
| [ORG-034-two-layer-goal-architecture-intent-first-justification.md](ORG-034-two-layer-goal-architecture-intent-first-justification.md) | Two-layer goal architecture — arc-intents as the missing justification tier | Adopted 2026-06-12 (operator-ratified live, Telegram msgs 3761–3767, "Make it so" msg 3767) | 2026-06-12 |
| [ORG-035-memory-portability-per-agent-baseline.md](ORG-035-memory-portability-per-agent-baseline.md) | Memory portability — adopt the per-agent canonical baseline, defer the shared tier | Adopted (Autonomous) 2026-06-13 by HO (Orchestrator); operator-ratifiable / override-available; one sub-decision (canonical store repo + visibility) RESERVED to the operator | 2026-06-13 |
| [ORG-036-dispatch-flow-default-self-spawn.md](ORG-036-dispatch-flow-default-self-spawn.md) | Dispatch-flow default — project-orchestrator self-spawns workers; HO pre-stages by exception | Adopted 2026-06-24 by Orchestrator (HO) under principles-as-ratification (below-goal-level dispatch-methodology decision per the operator msg 3733); D7 directive-text amendment drafted below SURFACED for the operator's async ratification. Resolves T-1292. | 2026-06-24 |
| [ORG-037-team-coordination-liveness.md](ORG-037-team-coordination-liveness.md) | Team-coordination liveness — turn-firing watchdog + bilateral pending-action self-checks | Ratified 2026-06-26 by the operator (Layer 3 actuation gates on the operator) | 2026-06-25 |
| [ORG-038-codex-system3star-assurance-auditor.md](ORG-038-codex-system3star-assurance-auditor.md) | Codex as Pantheon's System-3* assurance auditor | RATIFIED 2026-07-06 by the operator (T-1582, ticket-UI verdict 23:36:20Z) | 2026-06-29 |
| [ORG-039-harness-config-distribution-model.md](ORG-039-harness-config-distribution-model.md) | Harness-config distribution model — per-harness FRAMEWORK/INSTANCE split + instance.yaml binding + host-provisioning contract | Ratified 2026-06-30 by the operator (Telegram 3608/3609, via T-1474) — distribution/product axis, the harness-config sibling of ORG-029 | 2026-06-30 |
| [ORG-040-authority-provenance-model.md](ORG-040-authority-provenance-model.md) | Authority provenance — substance anchor, accidental-drift check, and the documented accepted-risk boundary | Adopted (HO, under amended D3; operator-digest-surfaced) | 2026-07-06 |
<!-- AUTOGEN-END spine-index -->
