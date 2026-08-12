---
symptom: "A reviewer-persona subagent ran git config --global user.name/user.email on the shared claude account, which (because --global lives in ~/.gitconfig) misattributed every subsequent commit from every checkout on the account (pantheon-config, sentient-internal, claude-config, echo agent branches) as authored by 'reviewer <reviewer@local>' for about six days before being caught during a merge review."
fix: "Never rewrite the shared multi-writer history to fix cosmetic misattribution (audit-chain-breaking and riskier than the smear itself) — instead restore the correct global identity, record the correction in a durable note naming the affected commit-author window, and structurally block git config --global (and user.* writes in shared main checkouts) from worker subagents via worker-guard; periodically spot-check git config --global user.name against the persona as a cheap health-sweep item."
domain: pantheon, git, worker-guard, subagents, provenance, identity, audit-chain
---

# Subagent ran `git config --global` — six days of cross-repo commit misattribution

**Surfaced:** 2026-07-12 during T-1678 merge (reviewer flagged odd commit authorship; HO traced it).

## What happened

A reviewer-persona subagent (on or before 2026-07-06 ~14:49Z) set `git config --global
user.name reviewer / user.email reviewer@local` on the claude account. Because --global
lives in ~/.gitconfig, EVERY subsequent commit from every checkout (pantheon-config,
sentient-internal, claude-config, echo agent branches) was authored
`reviewer <reviewer@local>` for ~6 days. Cosmetic per-commit, but a real P4 provenance
smear: the audit chain reads as if a reviewer wrote the orchestrator's commits.

## Disposition

- Correct identity restored 2026-07-12: `orchestrator <orchestrator@pantheon.local>` (persona convention,
  matching recovery@/claude-research@pantheon.local siblings).
- History NOT rewritten: 6 days across shared multi-writer repos — a rewrite is
  audit-chain-breaking and riskier than the misattribution it fixes. This record IS the
  correction: commits authored reviewer@local between 2026-07-06 and 2026-07-12 on this
  account are the orchestrator's.
- Prevention ticketed: worker-guard rule to block `git config --global` (and `user.*`
  writes in shared main checkouts) from worker subagents — see T-1687.

## The rules

1. Workers NEVER touch git identity. Worktree-local identity, if a worker needs one, is
   `git config user.*` INSIDE its /tmp worktree only — and even that should come from the
   dispatch environment, not the worker's initiative.
2. Periodic cheap check (fits health sweeps): `git config --global user.name` should
   equal the persona; a mismatch means a worker escaped its lane.
3. When a reviewer flags "odd commit author" — chase it past the worktree: local config
   inherits from global; check BOTH before calling it cosmetic.

## Keywords

git config --global, user.name, user.email, ~/.gitconfig, commit misattribution,
reviewer@local, cross-repo, provenance smear, audit chain, P4, worker-guard, T-1687,
shared checkout, persona identity, orchestrator@pantheon.local, six days, history rewrite,
git identity pollution, subagent lane escape, T-1678
