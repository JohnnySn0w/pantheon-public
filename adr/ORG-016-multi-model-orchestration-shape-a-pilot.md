---
aliases: [ORG-016]
title: Multi-model orchestration: Shape A pilot (Claude-primary + Grok/GPT as MCP-wrapped tools)
status: Ratified 2026-05-17
date: 2026-05-17
---
# ORG-016 — Multi-model orchestration: Shape A pilot (Claude-primary + Grok/GPT as MCP-wrapped tools)

**Status:** Ratified 2026-05-17 by the operator (Telegram msg_1629 "kk org16 ratified"). **Implementation CANCELLED 2026-05-17 by the operator (msg_1634 "nah just write it off. Close off this line of work.")** post-ratify: surfaced cost-of-acquisition (xAI API is a separate paid product from the operator's existing X Premium sub; was not accounted for at ratify time) didn't justify the pilot's value. T-0410 (server scaffold) + T-0411 (key custody) both closed as won't-do. Decision C (gpt-nano eval) status pending — structurally cancelled by default since Shape A pilot was its precondition; awaiting msg_1637 explicit verdict.

See §"Post-ratify cancellation" below for the honest record.

**Date authored:** 2026-05-17

**Author:** Orchestrator (Orchestrator) — decision-shape doc lifted from Specialist's research deliverable per the operator Telegram msg_1626 ("should 387 also be adr"). Specialist's analysis (`knowledge/watch/reports/2026-05-17/multi-model-orchestration.md`, 404 lines) is the supporting working-tier research; this ADR captures the binding ratification.

**Related:** [[T-0378]] (Specialist research dispatch), [[T-0387]] (ratification ticket, awaiting the operator), [[ORG-014]] + [[ORG-015]] (same-day precedents for working-tier → ADR promotion / ratifiable-class allow-list), `knowledge/watch/reports/2026-05-17/multi-model-orchestration.md` (supporting analysis), `knowledge/watch/reports/2026-05-15/anthropic-rate-limit-followup-programmatic-pool.md` (billing-surface stability reference for the deferral rationale).

**HITL:** Authored

## Context

Per the operator msg_1469+1470 (2026-05-17), HO lifted Specialist's above-the-bar recommendation set from the multi-model orchestration research dispatch into a ratification ticket (T-0387 filed earlier same day). Original T-0387 had `ratifiable_files: [pantheon-config:knowledge/watch/reports/2026-05-17/multi-model-orchestration.md]`; per [[ORG-015]] (just authored, awaiting ratify at T-0400) `knowledge/watch/reports/` is on the exclusion list for ratifiable_files — this ADR is the substrate correction.

Specialist's research (5 multi-model patterns, per-model differentiator table, function-calling shape mismatch analysis, cost + latency stacking, failure modes, three concrete Pantheon integration shapes) supports the decision below. The decision itself is the ratification surface.

## Decision

Three coupled sub-decisions, ratified together:

### Decision A — Pilot Shape A: `pantheon-mcp-grok` with X Search only

**Architecture**: Pantheon stays Claude-Code-on-Telegram (HO / Recovery / Specialist unchanged). Adds one MCP server initially:
- `pantheon-mcp-grok` — wraps `grok-4.3`'s Agent Tools, narrowed to **X Search only** for the v1 pilot.

**Concrete pilot use case**: Specialist's watch_runner watches Anthropic / xAI / OpenAI policy posts on X via Grok-X-search; flags material announcements into Telegram digest. Cost estimate: ~$0.025/turn ($5-15/month at low-volume polling); ~1-2 days HO-tier implementation effort.

**Why Shape A (vs B, vs C, vs status quo)**:
- Minimum architecture change. Pantheon's discipline (HERMES byte-sequence guard, narrow scope-asserted git, audit ledger, multi-instance Claude personas, persona files) all unchanged.
- No persona drift — Specialist stays Specialist regardless of which model handled the sub-task (vs Shape C's heavy persona-drift risk per §5.2 of the research).
- Capability access is per-task explicit (clear in the dispatch's tool-use log).
- MCP servers can be added/removed without changing Pantheon core (low commitment cost; easy reversal if pilot fails).
- Reliability + content-policy risk inheritance is LOW (Claude-substrate stays; we just call out for narrow tool surface).

**Trade accepted**: every Grok call still routes through Claude's context (Claude reads + summarizes Grok's result) — doesn't capture "Grok is cheaper at the whole task" wins that Shape B would. Acceptable for v1 because v1's question is *"does multi-provider tool-access help Pantheon at all,"* not *"can we shift task ownership to Grok."*

### Decision B — Defer Shape B/C decision pending Anthropic billing-surface stability

**Defer** Shape B (specialized non-Claude Python workers) and Shape C (new per-model VPS substrate instances) until at least 30 days post-2026-06-15 Anthropic programmatic-credit-pool rollout (see `knowledge/watch/reports/2026-05-15/anthropic-rate-limit-followup-programmatic-pool.md` for the billing-surface stability reference).

**Why defer**:
- The single-substrate billing surface (Claude subscription + the 2026-06-15 programmatic pool transition) is the load-bearing concern this quarter. Adding Shape B or Shape C multi-substrate complexity is premature until single-substrate billing surface stabilizes.
- Specialist's research §4.3 flagged "hidden costs of multi-provider" (~3× observability surface, separate audit chains, key-rotation surface multiplication) — these are real costs that don't pay back until Shape A pilot data proves multi-provider value at all.
- Reversal cost: lower for Shape A (one MCP server) than Shape B (Python cron workers with state-file conventions) or Shape C (full new VPS users + bots + ledger conventions).

Re-evaluate trigger: post-2026-07-15 (30 days post-cutover). If Shape A pilot has demonstrated value AND programmatic credit pool is comfortable on current researcher model usage, re-open Shape B/C decision.

### Decision C — Condition on (A) succeeding: evaluate `pantheon-mcp-gpt-nano` for cheap-classifier pre-filter

If Shape A pilot demonstrates MCP-wrapping value (Decision A first review at ~30 days post-launch), evaluate `gpt-5.4-nano` ($0.20 / $1.25 per 1M tokens input/output) as a cheap classifier pre-filter ahead of Specialist researcher dispatches.

**Concrete use case**: pre-classify watch-loop topics for "above-the-bar findings expected" before invoking Specialist's full researcher dispatch. Could materially reduce researcher dispatch volume on cheap-topic noise (estimated 15-25% reduction if real-world matches modeling; Specialist's research §3 + §6 cover the classifier shape).

**Why condition on (A)**: gpt-nano evaluation requires an MCP-server pattern (same shape as Shape A). If Shape A pilot fails or reveals operational tax that Shape A's pros didn't anticipate, the gpt-nano evaluation inherits those problems. Sequencing A → C is cheaper than both in parallel.

Re-evaluate trigger: Shape A pilot post-30d review verdict.

## Alternatives considered

| Alternative | Outcome | Why not |
|---|---|---|
| **Status quo** (Claude-only) | Implicit baseline | Specialist's research §1.1 documents 5 production patterns from Atlassian / Amazon Rufus / Stripe / LinkedIn / Shopify where multi-model capability-routing materially improves cost / latency / quality. Pantheon should at least pilot multi-provider tool-access. |
| **Shape A only** (no defer of B/C, no condition C) | Considered + rejected | Lacks the deferral discipline. Specialist's research §4 + §5 surface real multi-provider costs; committing to Shape B or C without pilot data is premature. Decision B is the disciplined sequencing. |
| **Shape B (specialized Python workers)** | Deferred per Decision B | Higher architecture cost; new systemd timers + state-file conventions + audit-chain additions. Bills against metered API directly (predictable spend) but operational tax is real. Defer until Shape A pilot data justifies the next step. |
| **Shape C (new per-model substrate instances)** | Deferred per Decision B | Heaviest architecture shift. Persona drift across providers is real (research §5.2). Grok-substrate carries Grok's reliability + content-policy volatility risks (MechaHitler 2025, CSAM-filter false-positives 2026). Not yet appropriate. |
| **OpenRouter gateway** (single-key abstraction over multiple providers) | Considered + rejected | Specialist's research §6 notes OpenRouter exists; Pantheon's discipline (per-provider audit chains, narrow scope-asserted git, multi-instance persona files) gains little from gateway abstraction. Direct MCP wraps stay observable. |

## Consequences

**Positive:**
- Pantheon gains multi-provider tool-access (Grok X Search) with minimum architecture change.
- Specialist's watch-loop gains a concrete multi-provider capability (X policy-post monitoring) that's plausibly material to research outputs.
- Path forward to gpt-nano cheap-classifier evaluation if Shape A succeeds.
- Defer discipline preserves the billing-surface stability concern; doesn't commit Pantheon to multi-substrate complexity until single-substrate stabilizes.

**Negative:**
- Adds an MCP server to the substrate. New audit surface (xAI API logs); new key custody (xAI API key in Pantheon vault).
- Latency stacking on Grok-X-search calls (Claude → MCP → Grok → MCP → Claude). For async watch-loop dispatches, not a UX problem; documented for future-real-time-channel consideration.
- Sets a precedent for Pantheon-wraps-non-Claude-as-MCP — operational cost of the second + third MCP server (gpt-nano via Decision C, etc.) compounds; need to keep the MCP-wrapping policy disciplined (don't wrap everything).

**Deferred:**
- Shape B/C decision (Decision B; post-2026-07-15 re-evaluation).
- gpt-nano cheap-classifier evaluation (Decision C; conditional on Shape A pilot 30d review).
- LiteLLM / OpenRouter gateway evaluation (research §6 gap-list item; not on critical path).

## Implementation path (post-ratification)

On the operator ratify of Decision A:
1. HO designs sub-ticket arc for `pantheon-mcp-grok` MCP server. Plan-first dispatch via implementer; worktree pre-staged per T-0357; `agent/<sub-ticket-id>` branch per [[T-0176]] convention.
2. Scope: one MCP server + one tool (X Search) + key custody in Pantheon vault + Specialist watch_runner integration point + cost-monitoring integration (per ORG-015's cost-monitoring-thresholds policy when ratified).
3. Pilot launch + 30-day observation window.
4. Re-evaluate at 30d: ratify Decision C / re-open Decision B / sunset Shape A if pilot showed no value.

## DECISIONS

- Three coupled sub-decisions ratified together (A pilot, B defer, C condition) — justifies via P1 (architectural sequencing — A unlocks C; B preserves billing-stability concern) + P6 (preserve tensions — defer discipline doesn't reject Shape B/C, just sequences them) — Specialist's recommendation set was already coupled; ratifying them separately would lose the sequencing logic.
- Shape A pilot scope narrowed to X Search only (vs broader Grok Agent Tools surface) — justifies via P7 + D6 (cheap-action pilot — start narrowest tool surface; expand if results warrant) — minimum-viable proves the MCP-wrap pattern; broader scope is reversible expansion.
- ADR substrate (not git mv from `knowledge/watch/reports/`) — justifies via ORG-012 (information-inflow bubble-up methodology — research-shape doc stays at knowledge/watch/reports/ as working-tier; decision-shape doc lives at adr/) + ORG-015 (ratifiable-class allow-list) — preserves Specialist's audit-chain at knowledge/watch/reports/ + gives decision a clean ADR shape rather than forcing research-shape doc into decision-shape via rewrite.
- Re-evaluate triggers named explicitly (Decision B at 2026-07-15+30d; Decision C at Shape A 30-day review) — justifies via P3 + P4 (decisions justify upward; explicit re-evaluate triggers preserve the chain across calendar time) — avoids the "deferred forever" failure mode.
- Alternatives table includes status-quo + OpenRouter explicitly — justifies via P3 (decisions justify upward; reviewers see what was considered + rejected) — operationalizes ORG-015's "explicit Status field + alternatives" ADR pattern.

## Post-ratify cancellation (2026-05-17)

**Chain:** msg_1629 ratify → HO files T-0410 (server scaffold) + T-0411 (xAI key custody operator coordination) → msg_1632 surfaces the operator's X Premium sub (cheap consumer tier) and his question whether it includes API access → HO researches + replies msg_1633 (X Premium ≠ xAI API; separate paid product; $175/mo data-sharing free credit option) → msg_1634 "nah just write it off. Close off this line of work." → HO closes T-0410 + T-0411 as won't-do, amends this ADR.

**Honest record:** the architectural decision was ratified on its merits; the cancellation is at the operator cost-vs-value tier, not a defect in the ADR's analysis. **Lesson surfaced**: future multi-substrate / multi-provider ADRs should foreground operator cost-of-acquisition (new account, new billing surface, key custody work) as a first-class consequence at draft time, not as a post-ratify discovery. Specialist's research §4.3 covered hidden multi-provider costs *in aggregate* but didn't surface the **operator-side acquisition surface** as a discrete decision input. Amending the ADR template (or ORG-012's information-inflow methodology) to add a §"Operator cost-of-acquisition" subsection before §Ratify trigger would close this gap — flagging as a candidate ADR-template tightening, not filing as ticket here since the lesson is more cleanly captured by next-draft application.

**Status of each sub-decision post-cancellation:**
- Decision A (Shape A pilot): **cancelled**.
- Decision B (defer Shape B/C): moot — entire multi-model line of work retired for now; no calendar trigger active.
- Decision C (gpt-nano eval): structurally cancelled by default (precondition Shape A succeeding is now N/A); awaiting the operator's explicit verdict per msg_1637.

**Re-open trigger:** if the operator's posture on multi-substrate cost-acquisition shifts (Anthropic policy change, new free-tier offering from xAI/OpenAI, Pantheon scope expansion that justifies the operator surface), HO refiles a fresh ratification ticket with the operator cost-of-acquisition surfaced upfront.

## Ratify trigger

T-0387 carries the ratification. `ratifiable_files` on that ticket updates to point at this ADR (replacing the prior `knowledge/watch/reports/...` path that landed before ORG-015 made the substrate explicit). On the operator's ratify verdict via Telegram:
- Status flips Authored → Ratified inline (header `**Status:**` line + adr/README.md row).
- HO designs the `pantheon-mcp-grok` sub-ticket arc (per Implementation path above).
- 30-day pilot observation window opens; Decision C + Decision B re-eval triggers calendar-tracked.

If the operator modifies any of A/B/C → HO edits this ADR in-place + re-files T-0387 for ratification (one round-trip).
