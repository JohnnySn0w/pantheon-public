# skills/

Reusable, self-contained skill bundles loaded on-demand by Claude Code agents. Each skill lives in its own subdirectory and ships a `SKILL.md` with YAML frontmatter (`name:` + `description:`) followed by the skill's full content.

Skills in this directory are scoped to Pantheon org operations (orchestration patterns, dispatch discipline, verification gates). Project-specific skills live in each project's `.claude/skills/` tree.

## When to add a skill

- A reusable procedure has been proven across ≥2 independent use-sites (HO + project-orchestrators).
- The content is genuinely universal (not project-specific or ticket-specific).
- Shipping it as a skill saves more than it costs: agents load skills on-demand, so adding a skill only helps if callers will actually request it.

## Skill file shape

Each subdir ships exactly one `SKILL.md`:

```
---
name: <skill-slug>
description: <one-sentence trigger — when to use this skill>
---

# <Skill name>

<full skill content>
```

## Contents

| skill | description |
|-------|-------------|
<!-- AUTOGEN-START spine-index -->
| [`dispatching-parallel-agents/`](dispatching-parallel-agents/) | Use when facing 2+ independent tasks that can run without shared state or sequential dependency. Pantheon-specific: covers Agent-tool parallel dispatch in the current session, watch_runner-style parallel researcher fan-out, Task subagent parallelism, and when to NOT parallelize (file-overlap risk, related-failure cascade, exploratory debugging). Vendor-forked from obra/superpowers, adapted to Pantheon vocabulary + 3-tier dispatch chain (HO ↔ project-orchestrator ↔ worker). Composes with D6 (plan-first), D7 (decision flow execution; 3-REBOUND budget), the WIP limit (≤5 concurrent agents per echo-prismatic CLAUDE.md). |
| [`subagent-driven-development/`](subagent-driven-development/) | Use when delegating implementation, review, research, or orchestration work to subagents. Covers brief design (self-contained context, scope, acceptance), failure-mode handling (timeout, drift, hallucination, brief-mismatch REBOUND cycles), synthesis across N subagent outputs, and the 5-phase signatures-first sequence. Vendor-forked from obra/superpowers, adapted to Pantheon's 3-tier dispatch chain (HO ↔ project-orchestrator ↔ worker). Composes with D6 (plan-first), D7 (decision flow + 3-REBOUND budget + chain-of-justification), D11 (third-party surface verification), the dispatching-parallel-agents skill (when-to-parallelize), the verification-before-completion skill (acceptance-gate discipline). |
| [`verification-before-completion/`](verification-before-completion/) | Use before claiming any work complete, fixed, or passing. Requires running verification commands and reading their output BEFORE making success claims. Vendor-forked from obra/superpowers, adapted to Pantheon vocabulary (P/D citations, smoke loops, wet-runs, REBOUND/DECISIONS). Composes with D5 (smoke verification discipline), D6 (plan-first protocol), D8 (HO self-discipline — cross-check claims pre-commit), D11 (third-party surface verification). |
<!-- AUTOGEN-END spine-index -->
