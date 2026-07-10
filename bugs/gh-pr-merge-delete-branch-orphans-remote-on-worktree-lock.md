---
symptom: "`gh pr merge N --merge --admin --delete-branch` prints `failed to delete local branch agent/T-NNNN: ... used by worktree` — the PR IS merged, but the REMOTE branch `origin/agent/T-NNNN` is ALSO left undeleted (orphaned), accumulating stale refs over many merge cycles"
fix: "After any worktree-locked merge, explicitly delete the remote ref: `git push origin --delete agent/T-NNNN` (verify merged first: `git merge-base --is-ancestor <sha> origin/main`). Periodically sweep: `git ls-remote --heads origin 'agent/*'`."
domain: git
---

## Symptom

The standard HO merge flow (§C) ends with:

```
gh pr merge N --merge --admin --delete-branch
```

When the agent branch is still checked out in a `/tmp/<repo>-T-NNNN` worktree (it always is at merge time — the reviewer just ran the smoke there), the command prints:

```
failed to delete local branch agent/T-NNNN: failed to run git: error: cannot delete branch 'agent/T-NNNN' used by worktree at '/tmp/<repo>-T-NNNN'
```

This is widely treated as **benign** ("the PR still merged — verify `gh pr view N --json state` == MERGED"), which is true. The trap: the same failure ALSO means the **remote** branch `origin/agent/T-NNNN` was never deleted. `gh` deletes local-then-remote (or aborts the cleanup on the local error); either way the remote ref survives. Over many merge cycles, orphaned `origin/agent/*` refs accumulate silently.

## Cause

`gh pr merge --delete-branch` performs branch cleanup AFTER the merge. The local-branch delete fails because git refuses to delete a branch that is the HEAD of a live worktree (the `/tmp` worktree from the dispatch). That error short-circuits the cleanup before (or instead of) the remote-ref delete. The merge itself is unaffected (it happened server-side); only the post-merge branch housekeeping is skipped.

Observed 2026-06-09: PRs #28 (T-1034) and #29 (T-1003) both merged cleanly, both printed the worktree-lock error, and `git ls-remote --heads origin 'agent/*'` later showed `agent/T-1003` + `agent/T-1034` still on origin — orphaned. (Prior-session PRs #26/#27 had their remotes cleaned, so the orphaning correlates specifically with the worktree-lock path.)

## Fix

The §C flow already says "delete remote ref if lingering" — this documents the *trigger* so the check actually gets run. After a worktree-locked merge:

1. Remove the worktree + local branch first (frees the lock):
   ```
   git worktree remove --force /tmp/<repo>-T-NNNN
   git branch -D agent/T-NNNN     # now succeeds (worktree gone)
   ```
2. Delete the orphaned remote ref (safety: confirm merged first):
   ```
   git merge-base --is-ancestor <branch-sha> origin/main && echo MERGED
   git push origin --delete agent/T-NNNN
   ```
3. Periodic sweep to catch any that slipped through:
   ```
   git ls-remote --heads origin 'agent/*'     # should be empty (or only legit parked branches)
   ```

Note: even removing the worktree before the `gh pr merge` does NOT help — `gh` deletes the LOCAL branch which still exists after worktree removal; the local-then-remote sequencing is the issue. The reliable cleanup is the explicit `git push origin --delete` above.

## Discovered

- 2026-06-09 (HO session, ledger entries -129/-132) — PRs #28/#29 merged; the worktree-lock error left `origin/agent/T-1003` + `origin/agent/T-1034` orphaned; caught during a proactive repo-hygiene sweep and cleaned via `git push origin --delete agent/T-1003 agent/T-1034` (both verified `--is-ancestor origin/main` first).

## Keywords

git, gh, gh pr merge, --delete-branch, --admin, orphaned remote branch, agent branch, worktree, used by worktree, cannot delete branch, ls-remote, push origin --delete, pantheon-config, merge flow, §C, stale ref
