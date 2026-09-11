---
aliases: [ORG-035]
id: ORG-035
title: "Memory portability — adopt the per-agent canonical baseline, defer the shared tier"
status: Adopted (Autonomous) 2026-06-13 by HO (Orchestrator); operator-ratifiable / override-available; one sub-decision (canonical store repo + visibility) RESERVED to the operator
date: 2026-06-13
---

# ORG-035 — Memory portability: adopt the per-agent canonical baseline, defer the shared tier

**Status:** Adopted (Autonomous) 2026-06-13 by HO (Orchestrator) — ADR-tier org-architecture, in HO's lane per msg-3733 ("everything below goal-level is yours") + explicit delegation (ledger `2026-06-12T18:55:14Z-201`, "memory architecture = ADR-tier, HO adopts"). The mandatory adversarial pass for a standing-rule proposal is satisfied by the codex second-opinion over the observed-harm analysis (T-1117/T-1122, 2026-06-13). **operator-ratifiable / override-available.** One sub-decision is RESERVED to the operator (§Reserved below: the canonical store's repo + visibility).
**Author:** Orchestrator (Orchestrator). Decides **T-0828 + T-0692 together** (the memory-portability direction the auto-consumer surfaced).
**Justifies via:** D10 (frugality — conditional-load + one source of truth, no substrate for an unobserved failure), D3 (act-with-adjustment — every step reversible: unset `autoMemoryDirectory` reverts), P1 (architecture over policy — fix the storage *location*, not the recall *algorithm*), P4 (provenance/traceability), P5 (cross-agent grants stay reserved).
**Re-evaluate:** when a shared-tier re-trigger fires (below), OR on a Claude Code memory-subsystem change that moves `autoMemoryDirectory`, OR if `rg`-over-corpus stops scaling (the BM25/embeddings "not yet" line).

## Context

File-based memory (`MEMORY.md` index + one-fact-per-file pins under `~/.claude/projects/<mangled-cwd>/memory/`) is a default we backed into, not a deliberate choice (the operator, T-0657). Three research passes (T-0089 Cofounder2, T-0385 Letta, T-0151 survey) all returned "defer until a threshold fires." Recovery's design pass (`notes/2026-06-03-portable-memory-store-design.md`, greenlit msg 2156) produced **Shape A**; Specialist's observed-harm gap analysis (T-1117) + the codex second-opinion (T-1122) supplied the evidence basis and two corrections. This ADR records the direction those converge on.

**The evidence that decides it (T-1117, codex-verified — supersedes the older "drift trigger has effectively fired" framing in T-0828's body):** cross-instance *semantic* drift — the headline worry (Letta crit #1: agents writing divergent, conflicting copies of one rule needing manual reconciliation) — has produced **zero observed-harm incidents** in a very-thorough ledger/lessons/incidents sweep. What *has* bitten is cheaper and local: (1) the cwd-keyed path fork (HO's `-home-claude` / `-home-claude-echo-prismatic` split — silent un-recall), (2) staleness/decay (incl. a 5-day dead-key outage), (3) memory↔persona boundary churn. The expensive worry has not fired; the cheap ones have.

## Decision

**Adopt Recovery's Shape A per-agent canonical baseline; explicitly DEFER the shared-reconciliation tier.**

1. **Per-agent canonical store via `autoMemoryDirectory`** (Shape A, design §4). A stable agent-keyed, version-controlled path; point the harness at it natively (no symlink hack). Kills the cwd-fork class (P1) and the launch-cwd non-portability. Recall algorithm unchanged (harness-internal, unreplaceable — design §2); we move the *location*, keep markdown-one-fact-per-file. Reject Shape B (structured DB + projection) for the baseline — it reintroduces the index-drift class and buys nothing before the deferred BM25 layer.
2. **grep → rg** (T-0692 part a): an **interactive-shell `alias grep='rg'`** setup-script step (NOT a `/usr/bin/grep` overwrite — `rg` is not behavior-preserving for scripts) + a `tools/memory_search.py` `rg`-backed CLI giving non-harness consumers a query path (P3).
3. **Schema lint + generated index** (P4): `tools/memory_lint.py` (frontmatter validation) + `tools/memory_index.py` (regenerate `MEMORY.md` from the files → kills hand-index drift). **This is also the operative mechanism for T-0872 ORG-012 §5 cross-user coverage** — the lint must cover HO's own pins, closing that gap without a cross-user grant.
4. **Provenance + freshness metadata, added NOW** (codex correction, the load-bearing one): frontmatter gains `owner`/`source`/`created`/`updated`/`verified`/`scope`. Without it the eventual shared-tier migration becomes "archaeology" (can't tell which agent owns which fact / when last verified). The mechanical lint covers broken-paths/missing-files/dup-names; a **separate freshness discipline** flags high-risk pin classes (credentials, flags, endpoints, ownership) whose `verified` is stale — the path-existence lint would NOT have caught the dead-key outage (the key existed textually but was revoked), so freshness is its own check.
5. **Codify the memory↔persona boundary** (gap 2): stable identity/role/standing-access ⇒ persona brief; accumulated feedback/project facts ⇒ auto-memory; a feedback pin promotes to the brief only when standing-access/identity-critical AND it has drifted out of working-context mid-session ≥ twice.

**DEFER the shared-tier / `.af` reconciliation substrate (P5).** It solves a problem with zero observed harm and would add write-arbitration + privacy cost for speculative benefit. **Re-trigger conditions** (any one): a cited cross-agent semantic-conflict incident that caused a wrong action; repeated manual cross-agent sync becoming a recurring cost; any fact becoming intentionally shared AND safety-critical; multiple agents needing to *write* (not just query) the same durable class. Keep Letta crit #1 on the watch list, not the build list.

## Reserved to the operator (one sub-decision)

**The canonical store's git repo + visibility (design §7.1).** Memory contains operational and possibly user-private content. Recovery recommends a **separate private `pantheon-memory` repo** (durability + provenance without widening pantheon-config's exposure); alternatives are local-only git or a private subtree. This is reserved because it concerns exposing potentially-private content + a new repo's remote/visibility — an access/secrets-adjacent call that is the operator's, not HO's. The rest of the baseline does not block on it.

## Consequences / rollout (sequenced)

- **Buildable now, HO-lane, no reserved deps** (operate on the current memory dirs): `memory_lint.py` + `memory_index.py` + `memory_search.py` + the `grep='rg'` alias. Filed as the baseline-tooling follow-up. Reversible (delete the tools).
- **Gated on the §Reserved decision:** stand up `<canonical>` repo, set `autoMemoryDirectory`, migrate per-agent (Recovery first / dogfood, then HO *with* the fork-merge, Orchestrator, Specialist). Each cutover is reversible (unset the setting → harness reverts).
- **Cross-agent note:** setting `autoMemoryDirectory` in another agent's `settings.json` is that agent's domain (ORG-027) — coordinate via the queue; HO does HO's own cutover.
- Document in `sop/standard/` (memory conventions) + update the CLAUDE.md memory section at cutover.

## Forward-links

- `notes/2026-06-03-portable-memory-store-design.md` (Shape A, full design + §7 open decisions).
- `knowledge/watch/reports/2026-06-12/agent-memory-personas-gap-analysis.md` (T-1117 observed-harm basis) + its codex second-opinion (T-1122 corrections).
- T-0872 (ORG-012 §5 coverage rides on the lint, item 3). Dei Object-Type co-design (T-0668/0669/0670 — a memory is a candidate Object Type; co-design the frontmatter schema when Dei lands). ORG-024 gap-8 (this is its baseline; BM25/embeddings stays "not yet").
