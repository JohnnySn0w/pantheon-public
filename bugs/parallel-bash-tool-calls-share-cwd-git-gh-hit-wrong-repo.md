---
symptom: "Several Bash tool calls issued in one response (or across turns) share ONE persisted cwd, and the harness resets it ('Shell cwd was reset to /home/claude'); a `cd` in one call redirects a `git`/`gh` call in another — e.g. `gh pr merge 18` intended for pantheon-pwa ran inside pantheon-config, read MERGED off the wrong repo, and the pwa branch was deleted while its PR auto-closed unmerged."
fix: "Never rely on cwd: always `git -C <repo>`, `gh -R owner/repo`, absolute script paths; put `cd` only inside the single compound command that needs it; verify a merge on the SAME repo (`gh -R … pr view N` + `git -C … ls-remote origin main`) before any branch delete. Recovery: `git branch agent/<t> <sha>` → push → `gh pr reopen` → merge."
domain: "claude-code bash tool, cwd persistence, git, gh, pantheon-pwa, pantheon-config, merge flow"
---

# Parallel Bash tool calls share ONE cwd — `git`/`gh` without an explicit target hits the wrong repo

**Surface:** Claude Code Bash tool, any session that issues several Bash calls in one response (or relies on cwd persisting across turns). Observed 2026-08-25 (Orchestrator, T-3220 landing).

## Symptom

A `gh pr merge 18 --squash` intended for `pantheon-pwa` ran inside `pantheon-config` (whose long-merged PR #18 answered "already merged"), the follow-up `gh pr view 18` reported MERGED off the wrong repo, and the next step deleted the pwa head branch — GitHub auto-CLOSED the real (unmerged) pwa PR #18. Same race pushed pantheon-config from the pwa cwd ("Everything up-to-date" — on the wrong repo). Nothing was lost (the commit was still in the local object store) but recovery cost ~10 min: `git branch agent/<t> <sha>` → push → `gh pr reopen` → merge.

## Cause

The harness keeps a single shell; a `cd` inside one compound command is shell STATE, not scope. Sibling calls in the same response interleave, so whichever `cd` runs first wins for all of them, and the cwd also persists into later turns.

## Fix (discipline)

- Every `git` call: `git -C /home/claude/<repo> …`. Every `gh` call: `gh -R <owner>/<repo> …` (or a `cd` inside the SAME compound command, immediately before it).
- Verify a merge on the SAME repo before any branch delete: `gh -R <owner>/<repo> pr view N --json state,mergeCommit` AND `git -C <repo> ls-remote origin main` advanced. Never chain a delete after a merge in one command (see `feedback_never_chain_branch_delete_after_merge`).
- The only legitimate cwd dependency is Agent-tool worktree placement (`isolation: "worktree"` follows the Bash cwd's repo): set it with a dedicated `cd` call immediately before the dispatch, nothing else in that response.

## Recovery if it bites

`git -C <repo> cat-file -t <sha>` (commit survives branch deletion until gc) → `git -C <repo> branch agent/<t> <sha>` → `git -C <repo> push origin agent/<t>` → `gh -R … pr reopen N` → merge → verify as above.

Related: memory pin `feedback_parallel_bash_calls_share_cwd_use_explicit_paths`; `feedback_never_chain_branch_delete_after_merge`.
