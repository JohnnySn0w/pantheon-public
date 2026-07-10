---
name: dispatching-parallel-agents
description: Use when facing 2+ independent tasks that can run without shared state or sequential dependency. Pantheon-specific: covers Agent-tool parallel dispatch in the current session, watch_runner-style parallel researcher fan-out, Task subagent parallelism, and when to NOT parallelize (file-overlap risk, related-failure cascade, exploratory debugging). Vendor-forked from obra/superpowers, adapted to Pantheon vocabulary + 3-tier dispatch chain (HO ↔ project-orchestrator ↔ worker). Composes with D6 (plan-first), D7 (decision flow execution; 3-REBOUND budget), the WIP limit (≤5 concurrent agents per echo-prismatic CLAUDE.md).
---

# Dispatching parallel agents

**Status:** **Ratified 2026-05-27 by the operator.** Authored 2026-05-27 by Orchestrator (Orchestrator). — vendor-forked from [obra/superpowers — dispatching-parallel-agents](https://github.com/obra/superpowers/blob/main/skills/dispatching-parallel-agents/SKILL.md) and adapted to Pantheon vocabulary. Closes T-0407.

**HITL:** Authored; awaiting the operator ratification.

**Adaptation:** Cross-references Pantheon P/D ids + the 3-tier dispatch chain (HO ↔ project-orchestrator ↔ worker). Includes Pantheon-specific guidance on the four dispatch surfaces (Agent tool / Task subagent / watch_runner spawn / `claude -p`), the WIP-limit-of-5 convention, and file-overlap serialization decisions. Concrete examples drawn from Pantheon's own dispatch history.

**Re-triage note (T-0407 trigger):** Original triage required a concrete dispatch-shape mistake costing >1h. No single mistake fired that bar in isolation, but the 2026-05-26→27 Echo arc accumulated enough dispatch-shape decisions (T-0616 serialization vs T-0617 to avoid file overlap; researcher+implementer phase interleaving on T-0615; the 5-phase signatures-first methodology that emerged from msg_2371) that codification is no longer speculative. The discipline IS being practiced; this skill captures the operating pattern.

## Overview

You delegate tasks to subagents with isolated context. The choice between **parallel** and **sequential** dispatch is a design decision with concrete cost / risk tradeoffs. This skill codifies the decision.

**Core principle:** dispatch one agent per independent problem domain when concurrency saves wall-clock time AND the agents won't interfere. Serialize when domains overlap, when the next dispatch's brief depends on the previous dispatch's output, or when concurrent file-edits would collide.

## Pantheon's dispatch surfaces (which tool to use)

Four dispatch mechanisms with different parallelism characteristics:

| Surface | Use for | Parallelism | Notes |
|---|---|---|---|
| **Agent tool** (in-session subagent) | Focused work within the current session: implementer, reviewer, researcher, code-reviewer. Subagent has its own context, returns a single message. | Multiple Agent calls in one message run concurrently. WIP limit ≤5 per echo-prismatic CLAUDE.md. | Subagents don't see the dispatcher's transcript — briefs must be self-contained. |
| **Task subagent (Task tool)** | Same shape as Agent for in-session work. Older API surface; the Agent tool is preferred for new work. | Parallel via multiple calls in one message. | Same self-contained-brief rule. |
| **`watch_runner.py` spawn** (claude-cli) | Specialist-style automated researcher fan-out (5-6 researchers per fire). Spawned as separate processes via claude-cli. | Parallel by design. | Each subagent runs in its own process; less coordination overhead but no shared context. |
| **`claude -p` (Claude Code primary cli)** | One-shot prompts with no interactive session. `ho_resumer.py`-style. | Sequential (one process per invocation). | **Cost-of-acquisition note (msg_kino_sunset 2026-06-13):** headless `claude -p` bills API as of 2026-06-13 — cost-no-go after that date for any unattended use. Re-evaluate the chosen surface for any post-cutoff work. |

## The Iron Question

Before parallelizing: **Are the dispatched tasks genuinely independent?**

- Will they edit the same files? (If yes, serialize OR pre-stage worktrees that isolate the edits.)
- Does dispatch B's brief need dispatch A's output? (If yes, serialize.)
- Are the bugs related such that fixing one might fix the others? (If yes, serialize — start with the most-load-bearing.)
- Do they share a finite resource (single live API session, single mic, single daemon process)? (If yes, serialize.)

If any of these fire, serialize. Otherwise parallelize.

## Pantheon parallelization patterns (when to use)

### Pattern A — multiple independent reads (always parallelize)

When investigating an unknown area, multiple read-only operations (Read, Grep, Glob, WebFetch, Bash for ls/cat) are independent. **Always batch into one message.**

```
Good: Read(file_a) + Read(file_b) + Grep(pattern) in a single message — 3 tool calls run concurrently
Bad:  Read(file_a) → wait → Read(file_b) → wait → Grep(pattern)
```

This is the cheapest parallelization win. No coordination cost; no file-overlap risk; just time saved.

### Pattern B — multiple independent researcher dispatches (parallelize)

Specialist's `watch_runner.py` dispatches 5-6 researcher subagents per fire, each on a different research question. Each researcher is independent (its own URL set, its own scope, its own output file). Parallelization is structural — embedded in `watch_runner.py`'s design.

For HO-driven research dispatches: parallelize when researching N orthogonal questions for a single decision (e.g., "compare Letta vs MemGPT vs Cursor's memory architectures" → 3 parallel researchers, each on one). Synthesize after.

### Pattern C — implementer + reviewer (always serialize)

The reviewer reviews the implementer's diff. Reviewer cannot start before implementer commits. Always serial.

### Pattern D — multiple tickets touching the same orchestrator (serialize)

When two tickets both touch `crates/echo-orchestrator/`, serialize their implementer dispatches. Example: T-0616 (classifier entity-id fix) and T-0617 (orchestrator system_response on action_results errors) both touch the orchestrator. Dispatched serially: T-0616 lands first; T-0617 unblocks via `depends_on` cascade-clear. Reasoning: parallel edits would conflict at merge; serial cost is one extra wall-clock cycle.

### Pattern E — research + implementation phases (interleavable)

For research-first tickets (T-0615 was an example): researcher dispatches Phase 1 (docs survey + notes file). HO reads notes + decides direction. Implementer dispatches Phase 2 (codify the finding). These phases naturally serialize — the implementer's brief depends on the research output. But: the *next* ticket's research can start while the current ticket's implementation runs, if they're independent.

## Brief discipline (self-contained, per D7 chain-of-justification)

Subagents don't see the dispatcher's conversation. Every dispatch brief MUST include:

- **Scope:** specific files / specific question / specific ticket id
- **Context:** why this work exists; cross-references to related tickets; concrete failure-evidence
- **Constraints:** what NOT to touch (hand-curated docs, orthogonal areas, scope creep)
- **Acceptance bar:** what "done" looks like; D11 wet-run gates if applicable
- **DECISIONS requirement:** every non-obvious call must justifies-via P/D-id (per chain-of-justification rule in `~/CLAUDE.md`)
- **Plan-first directive** (D6): return plan as first message; await HO approval; no code before approval
- **HERMES byte-sequence escape rule:** in any prose referencing the trigger, escape the period (`HERMES\.md`)
- **5-phase signatures-first sequence** (msg_2371; ratified 2026-05-26): plan → signatures+docstrings → tests → code → smoke+wet-run+ship. Surface phases in the report even if collapsed inline for small changes.

For parallel dispatches: each brief is independently self-contained. Don't cross-reference between dispatches' briefs — the subagents can't see each other.

## Common mistakes

| Anti-pattern | Why it fails | Fix |
|---|---|---|
| "Fix all the tests" (one agent) | Agent gets lost across multiple domains | Split: one agent per test file / per subsystem |
| Parallel dispatch on overlapping files | Merge conflicts; one agent's diff overwrites another's | Serialize via `depends_on` gate |
| Parallel dispatch when next brief depends on prior output | Second agent's brief is incomplete | Serialize: get the prior output, then brief the next |
| Briefs referencing the dispatcher's conversation | Subagent can't see it | Self-contained briefs with quoted context |
| WIP > 5 concurrent agents | Coordination cost exceeds parallelization gain; HO can't track | Honor the limit (echo-prismatic CLAUDE.md) |
| Forgetting `claude -p` is billed post-2026-06-13 | Unexpected cost | Prefer Agent-tool subagents for any post-cutoff work; reserve `claude -p` for paid-equivalent use cases |

## When NOT to parallelize

- **Failures are related.** If fixing bug A might fix bugs B + C, start with A; don't fan out yet.
- **Exploratory debugging.** When the scope is unknown, one agent in deep can investigate; N agents in shallow lose the thread.
- **File-overlap.** Multiple agents editing the same file = merge pain. Pre-stage worktrees if you must parallelize; otherwise serialize.
- **Shared resource contention.** Single mic, single live API session, single daemon process = serial by design.
- **Plan-tier work.** The first dispatch in an arc usually surfaces decisions the rest depend on. Don't pre-fan-out before the plan lands.

## Real Pantheon example — when serialization was right

**Echo arc 2026-05-26:** T-0616 (Claude classifier entity-id fix, touches `echo-adapter-claude/`, `echo-adapter-ha-mcp/`, and orchestrator wire-through) + T-0617 (orchestrator system_response on action_results errors).

Both tickets touch the orchestrator. Parallel dispatch would have produced conflicting diffs at merge — both agents editing the same files in different ways. Decision: serialize. T-0616 dispatched first; T-0617 gated via `depends_on: [T-0616]`. ORG-013 cascade-unblock would clear the gate on T-0616 resolution. Cost: one extra wall-clock cycle. Saved: merge-conflict resolution + the REBOUND-cycles that file-overlap typically produces.

Decision justifies-via P5 (roles don't hop — file-overlap risk is the dispatcher's call, not the implementer's) + D7 (3-REBOUND budget — serializing protects the budget by preventing predictable conflict-cycles).

## Real Pantheon example — when parallelization was right

**T-0378 (Specialist's research arc):** dispatched 6 parallel general-purpose subagents in a single fire, each investigating a different orthogonal research question. No shared state; no file-overlap; each subagent's output was an independent notes file. Total wall-clock: ~30 min for all 6. Sequential would have been ~3 hours.

Decision justifies-via P1 (parallelization is the architectural property when domains are independent) + D10 (context-frugality — each subagent's narrow scope reduces dispatcher's tracking burden).

## Verification checklist (compose with verification-before-completion skill)

Before claiming "all parallel dispatches landed":

1. ✓ Each subagent returned its plan / report
2. ✓ No file-conflicts when reviewing all diffs together
3. ✓ Each diff's smoke loop runs green
4. ✓ Workspace smoke loop runs green AFTER all diffs landed (catches cross-cutting regressions)
5. ✓ Each DECISIONS section justifies-via P/D-ids per chain-of-justification
6. ✓ Wet-run probes for each dispatch covered the boundary the change touches (D11)

If any step is missing evidence: don't claim done; surface the gap.

## Composition with existing P/D + skills

- **D6** (plan-first): per-dispatch, not per-fan-out. Each parallel dispatch needs its own plan-tier approval.
- **D7** (decision flow): the 3-REBOUND budget is per-ticket, not per-fan-out. Serializing tickets that share file scope protects the budget by preventing predictable conflict-cycles.
- **D10** (context-frugality): narrow subagent scope reduces dispatcher's tracking burden.
- **D11** (third-party surface verification): each subagent's wet-run gates its own merge; parallel dispatches each pay their own D11 cost.
- **`verification-before-completion` skill**: each parallel dispatch's "done" claim follows the iron law of fresh evidence. The verification skill applies per-dispatch, not per-fan-out.
- **P5** (roles don't hop): the dispatcher decides serialization vs parallelization; the subagent decides what's in scope for its own task. Don't conflate these.

## Bottom line

Parallelize when domains are independent. Serialize when they overlap. The cost of getting this wrong is REBOUND-cycles, merge conflicts, and the kind of cognitive juggling Pantheon's WIP-limit-of-5 is designed to prevent.

When in doubt: one dispatch in deep beats N dispatches in shallow. Fan out after the first dispatch surfaces what the others need to know.
