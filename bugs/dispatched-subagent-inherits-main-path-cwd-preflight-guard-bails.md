---
symptom: "You dispatch an implementer/reviewer via the Agent tool with a pre-staged /tmp worktree named in the brief, and the worker HARD-BAILS on its cwd pre-flight: `git rev-parse --show-toplevel` returns the CANONICAL MAIN CHECKOUT (e.g. /home/claude/pantheon-config), not the worktree. The worker refuses to `cd` per brief prose — its guard checks session-START cwd and bails before running any other command. The Agent-tool subagent inherits the DISPATCHER's cwd, which is a main path whenever HO has been running Bash inside the repo; brief prose ('cd into /tmp/<worktree> as your first action') does NOT change where the session starts."
fix: "Dispatch with the Agent tool's `isolation: \"worktree\"` — the harness lands the agent in a fresh linked worktree under `.claude/worktrees/` (NOT a canonical main path), so the pre-flight guard passes. Brief the agent to (1) confirm `git rev-parse --show-toplevel` is not the main path, (2) `git branch -m agent/<task>` to rename the harness-auto branch to the org convention, (3) work/commit there, never push. Do NOT rely on 'cd /home/claude before dispatch' (the older reference_reviewer_cwd_tmp_worktree_dispatch pin) — the subagent inherits the dispatcher's ACTUAL cwd, not a value you can pin, and the Bash tool's own cwd resets to /home/claude between calls, so you cannot reliably steer the subagent into a pre-staged /tmp worktree by cd-ing your own shell. A manually pre-staged `git worktree add /tmp/<repo>-<ticket> -b agent/<task>` still fails because the subagent never lands there. Sub-gotcha: an `isolation:worktree` dir can VANISH mid-run (auto-clean races); if it does before any commit, the branch survives in refs — recover with `git worktree add /tmp/<repo>-<ticket> <branch>` and re-run pre-flight from /tmp."
domain: pantheon, dispatch-pattern, claude-code-harness, worktrees, subagents, implementer, reviewer, isolation-worktree
---

# Dispatched subagent inherits a main-path cwd; the pre-flight guard hard-bails

## Observed (T-2709 implementer + reviewer, 2026-08-11)

HO pre-staged `git worktree add /tmp/pantheon-config-T2709 -b agent/T-2709`, named it in the brief, and told the implementer to `cd` there as its first action. The implementer's guard ran `git rev-parse --show-toplevel` as its literal first command, got `/home/claude/pantheon-config` (a canonical main path), and **hard-bailed without taking any action** — correctly, per its pre-flight discipline ("a hard bail regardless of brief prose telling me to cd manually").

Root cause: the Agent-tool subagent's session cwd = the dispatcher's cwd at spawn time. HO had been running Bash inside `~/pantheon-config`, so the subagent inherited that main path. The pre-flight guard binds on session-START cwd, so no amount of "cd into the worktree" prose in the brief helps — the guard fires first.

## Why the older "cd /home/claude before dispatch" pin is insufficient here

`reference_reviewer_cwd_tmp_worktree_dispatch` says "cd /home/claude before dispatch; brief worker to cd into /tmp worktree." Two problems observed this session:
1. The Bash tool's cwd **resets to /home/claude between calls** (a bare `cd /tmp/...` does not persist), so you cannot reliably position your own shell — and even if you could, the subagent may not inherit it.
2. The subagent landed in `/home/claude/pantheon-config` (the dispatcher's working repo), not `/home/claude`, so it inherited the working-repo cwd, not a neutral one. A neutral `/home/claude` start might pass the "not a main path" check but still leaves the worker not IN the worktree, which its guard also wants.

## The reliable fix: `isolation: "worktree"`

Re-dispatched with `Agent({subagent_type: "implementer", isolation: "worktree", ...})`. The harness created a fresh linked worktree (`.claude/worktrees/agent-<id>`), landed the agent there — not a canonical main path — and the guard passed. Brief the agent to `git branch -m agent/<task>` as its first git action so the harness-auto branch takes the org `agent/<task>` convention; then commit there (never push), and HO pushes + PRs the branch afterward (refs are shared with the main checkout, so `git diff origin/main..agent/<task>` and `git push origin agent/<task>` work from HO's main worktree).

## Sub-gotcha: the isolation worktree can vanish mid-run

In the same arc the `.claude/worktrees/agent-<id>` dir disappeared from disk mid-session (an auto-clean race — isolation worktrees are "auto-cleaned if unchanged"). Because no commits existed yet, the `agent/T-2709` branch survived in the repo's refs; the worker recovered by `git worktree add /tmp/pantheon-config-T-2709 agent/T-2709` and re-running pre-flight from `/tmp`. Detection tell: a `git rev-parse --show-toplevel` that suddenly errors or a worktree path that no longer exists.

## Relationship to the sibling dispatch-cwd bug

Distinct from `bugs/worker-preflight-passes-then-absolute-path-drift-to-main-checkout.md`: there the pre-flight PASSES and the worker later composes absolute paths into the main checkout (per-call path drift). Here the pre-flight FAILS at spawn because the session started in a main path. The two are the spawn-side and the per-call-side failure modes of the same dispatch-cwd class.
