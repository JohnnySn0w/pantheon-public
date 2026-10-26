---
symptom: "A reflex `git stash` / `git stash pop` in an agent worktree pops or conflicts with ANOTHER arc's stash entry — git's stash stack is REPO-WIDE (one per .git), not per-worktree. Observed: an empty `git stash` followed by `git stash pop` consumed an unrelated arc's WIP-snapshot stash, causing merge conflicts in files that arc never touched."
fix: "Never bare `git stash`/`pop`/`apply` in a shared-.git worktree. Prefer a throwaway commit (branch-scoped) over a stash (repo-wide). If you must stash, tag it (`git stash push -u -m <unique-tag>`) and apply by message (`git stash apply 'stash^{/<unique-tag>}'`), verifying with `git stash list` first. Recovery from a bad pop: `git reset --hard HEAD && git clean -fd` (a CONFLICTED pop does not drop the foreign entry, so it survives)."
domain: "git / worktree / dispatch-discipline"
---

# git stash is repo-wide (one per .git), not worktree-scoped → cross-arc collision

## Symptom
In a linked worktree used for agent-arc isolation (`/tmp/<repo>-T-NNNN`, sharing the canonical `.git`), a reviewer/implementer reflexively runs `git stash` then `git stash pop`. The `pop` restores — or conflicts with — a stash entry created by a **different arc in a different worktree**.

Concrete (T-3185 review, 2026-09-13): an empty `git stash` (nothing local to stash) followed by a reflex `git stash pop` popped an unrelated `T-3039 WIP snapshot before baseline capture` stash, producing merge conflicts in `tools/chronicle.py`, `tools/test_chronicle.py`, `tools/graveyard.py` — files the reviewer's own arc never touched.

## Cause
`git stash` is **repo-wide, not worktree-scoped.** All linked worktrees share one `.git` directory and therefore one stash reflog (`refs/stash`). A `stash push` from any worktree pushes onto the single shared stack; a `stash pop`/`apply` from any worktree operates on the top of that shared stack — which may belong to another arc. There is no per-worktree isolation of the stash stack. (Stash analogue of the other shared-`.git` hazards: `git-common-dir-global-cache-poisoned-by-mocked-subprocess.md`, `ticket-cli-in-agent-worktree-merges-main-into-branch.md`.)

## Fix
- **Prevention (default): don't bare-stash in a worktree at all.** Use a throwaway commit — commits are branch-scoped, stashes are not: `git add -A && git commit -m wip` (undo with `git reset --soft HEAD~1`).
- **If you must stash, tag and apply-by-message — never bare, never by index:**
  - `git stash push -u -m '<unique-arc-tag>'`
  - `git stash list`  ← confirm YOUR tag is present and which entry it is
  - `git stash apply 'stash^{/<unique-arc-tag>}'`  ← by message match, NOT `stash@{0}` (index 0 may be another arc's)
- **Recovery** (you already popped a foreign stash and hit conflicts): `git reset --hard HEAD && git clean -fd` — fully restores the worktree to HEAD. A **conflicted** `stash pop` does NOT drop the stash entry, so the foreign arc's WIP survives (verified below).

## Verification
The shared-stack fact (two worktrees, one `.git`):
```
git -C /tmp/wt-A stash push -u -m arcA-wip
git -C /tmp/wt-B stash list      # shows "...: arcA-wip" — worktree B can pop worktree A's stash
```
Observed collision + recovery (T-3185, 2026-09-13): reviewer in one worktree popped the T-3039 arc's stash from another; `git reset --hard HEAD && git clean -fd` restored cleanly, and `git stash list` afterward confirmed the T-3039 entry was **not** consumed (a conflicted pop leaves it in place).

## Discovered
- T-3661 (this entry) — from the T-3185 reviewer incident, 2026-09-13.

## Keywords
git stash, git stash pop, git stash apply, stash reflog, refs/stash, worktree, linked worktree, shared .git, git common dir, repo-wide stash, cross-arc stash collision, reviewer discipline, WIP snapshot, git reset --hard, git clean -fd, dispatch-discipline, worktree isolation, throwaway commit
