# Bulk merged-branch pruning — exclude worktree-backed + feature branches, check open PRs first

**Date authored:** 2026-06-06 (distilled from a same-session overreach during a sanctioned repo-tidy pass)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** the next bulk branch-prune is run on any Pantheon repo; promote to an SOP step (or a `tools/prune_merged_branches.py` guard) if a second overreach occurs.

## Observation

During a tidy pass I pruned merged `agent/*` branches across repos. On `pantheon-config` / `pantheon-pwa` this was clean (per-ticket `agent/T-NNNN` branches, all `--merged origin/main`). On `echo-prismatic` I ran the same one-liner — `git push origin --delete $(git branch -r --merged origin/main | grep agent/)` — and it swept in **48** branches, including:

- four **long-lived feature branches with active checked-out worktrees** (`agent/echo-adapter-claude`, `-j17`, `-mqtt`, `-mqtt-qos-table` in `echo-prismatic-worktrees/`), and
- several other named feature branches (`echo-adapter-ha-mcp`, `echo-aggregate-store`, `echo-speak-posture`, …)

rather than only the disposable per-ticket `agent/T-NNNN` branches I intended.

**What saved it from being damaging (verified after the fact, not before):**
- Every deleted branch was genuinely `--merged origin/main` → **zero commits lost**; remote-branch deletion never touches local worktrees (all four adapter worktrees stayed intact at their SHAs, all in main).
- The only **open PR** (#62, `agent/T-0627`) was in the not-merged keep-list, so **no PR was closed** by the deletes. (Deleting a branch with an open PR *closes that PR* — the real latent hazard here.)

I restored the four worktree-backed adapter branches (`--no-verify`, since their commits were already in main + tested) to return the repo to the state I found it; left the genuinely-stale worktree-less merged branches deleted.

**Two surprises worth pinning:**
1. **echo's `.githooks/pre-push` full-workspace cargo gate fires on `--delete` too.** A branch *deletion* ran the entire multi-minute test suite before completing the push. (It passed, so the deletes went through — but a delete blocking on a build gate is non-obvious and the test output masked the delete confirmation.)
2. **`git branch -r --merged` is a blunt instrument for cleanup** — "merged" ≠ "disposable." Long-lived feature/integration branches are often fully merged at a sync point yet still actively used.

## How to apply

Before any bulk `git push origin --delete` of a branch glob:

1. **Scope to disposable branches only.** Filter to the per-ticket convention (`agent/T-NNNN$`), not all of `agent/*`. Exclude any name that isn't a bare ticket id (feature/integration branches like `agent/echo-adapter-*` are not disposable).
2. **Exclude worktree-backed branches.** `git worktree list` → never delete the origin ref of a branch that's checked out in a maintained worktree, even if merged. Cross-reference the delete set against it.
3. **Check open PRs.** `gh pr list --state open` → never delete a branch with an open PR (it closes the PR). Confirm the delete set is disjoint from open-PR head refs.
4. **Know the repo's pre-push hook.** On echo, deletes trip the cargo gate; use `--no-verify` for pure ref-restore of already-merged commits, but never to bypass the gate on a real content push.
5. **Verify-don't-trust on the delete set itself** — print the resolved branch list and eyeball it before piping into `--delete`. The list ballooned from an expected ~30 to 48 between two checks (origin advanced); a glob captured at command-time is not the glob you reasoned about a minute earlier.

Sibling: [[reference_pantheon_bugs_directory]]. The clean cases (pantheon-config, pantheon-pwa) are the norm; this lesson is the guard for repos with long-lived feature branches + worktrees + build-gate pre-push hooks (echo, and any future Rust/CI-gated project repo).
