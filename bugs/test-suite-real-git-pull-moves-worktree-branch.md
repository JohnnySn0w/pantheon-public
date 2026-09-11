---
symptom: "a dispatched worker's agent-worktree branch silently fast-forwards from origin/main mid-build (reflog: 'merge refs/remotes/origin/main: Fast-forward' it never ran) — its test surface / base moves under it"
fix: "patch tools.activity_digest.pull_repo_or_warn in every main()-path test (landed T-2898); pattern rule: any test exercising a tool's main() must mock its repo-sync call — grep pull_repo_or_warn/_pull_if_needed callers when adding main()-path tests"
domain: git, worktree, tests, dispatch
---

# Test suite ran a REAL git pull — moving the checked-out branch of whatever worktree ran it

## Symptom detail
bt1-builder (T-2841, 2026-08-12): 4 unexplained fast-forwards of `agent/T-2841` during its build; an unrelated main-side commit merged into its base mid-flight and turned its land-bar red with defects not its own. The worker never ran `git pull`.

## Root cause
`activity_digest.main()` calls `pull_repo_or_warn(...)` (→ real `git fetch origin main` + `git merge --ff-only refs/remotes/origin/main`, cwd-resolved to the repo the code runs from). Three `main()`-path tests in `tools/test_activity_digest.py` patched gather/send/ledger but NOT the sync — so every suite run executed a real fetch+merge in the running repo. On the main checkout: an invisible sync. In an agent worktree: the checked-out feature branch fast-forwards. Also a hidden network dependency in the suite.

## Verification
Reproduced deterministically: worktree on a branch 3 commits behind origin/main → `python3 -m unittest tools.test_activity_digest` → branch fast-forwards (reflog `merge refs/remotes/origin/main: Fast-forward`). Post-fix re-run in the same setup: branch does not move. Isolation was module-by-module elimination (test_git_utils / inbox_consumer / auto_commit / deploy_hook / pre_push all clean).

## Watch for recurrence
Any tool with a sync-at-startup (`pull_repo_or_warn`, `_pull_if_needed`) gaining a `main()`-path test re-creates this. The probe recipe above is the 30-second check.

## See also
[`lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md`](../lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md) — this bug is one of three that ground the pattern-tier lesson (with the ticket-cli-in-agent-worktree and live-hook-rollout-strands sibling bugs).
