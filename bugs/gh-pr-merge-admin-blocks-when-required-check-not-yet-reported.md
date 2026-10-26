---
symptom: "`gh pr merge <N> --repo <owner/repo> --squash --admin --delete-branch` fails right after opening a new PR with GraphQL error 'Required status check \"<check>\" is expected. (mergePullRequest)'. The PR shows mergeStateStatus: BLOCKED, mergeable: MERGEABLE — mergeable in the ref sense but blocked because a required check hasn't been reported at all yet (not even as pending). --admin bypasses check-*failure* not check-*absence*."
fix: "Wait for the workflow to register the required check (any status — pending is enough), then re-run the same `gh pr merge --admin` command. `gh pr view <N> --json mergeStateStatus` flipping from BLOCKED to CLEAN is the reliable signal admin-merge will succeed. Typical delay is 30s-2m between PR-open and check-registration. Do NOT --ignore-required-checks (no such flag) or disable branch protection — racy window is short and natural wait costs nothing."
domain: "github, gh-cli, pr-merge, branch-protection, required-checks, admin-bypass, racy-window"
---

# gh pr merge --admin blocks when a required status check has not yet been reported

## Symptom

`gh pr merge <N> --repo <owner/repo> --squash --admin --delete-branch` fails immediately after opening a new PR with:

```
GraphQL: Required status check "ci-gate" is expected. (mergePullRequest)
```

The PR shows `mergeStateStatus: BLOCKED, mergeable: MERGEABLE` — mergeable in the ref sense, but blocked because the required check has not been reported at all yet (not even as pending). `--admin` bypasses check-*failure*, not check-*absence*.

## Cause

Branch protection with required status checks needs each required check to have *reported at least one status* (pending / success / failure) on the merge commit's HEAD SHA before merge can proceed. A brand-new PR whose CI workflow has not yet fired hasn't produced any status entries yet — GitHub reads that as the check being "expected but unreported" and refuses to merge, admin or not.

## Fix

Wait for the workflow to register the required check (any status; even queued/pending is enough), then re-run the same `gh pr merge --admin` command. Typical delay is 30s–2m between PR-open and check-registration. `gh pr view <N> --json mergeStateStatus` flipping from `BLOCKED` to `CLEAN` is the reliable signal that admin-merge will now succeed.

Do NOT force it (there is no `--ignore-required-checks` flag on `gh pr merge`), and do NOT temporarily disable branch protection — the racy window is short and the natural wait costs nothing.

## Verification

Hit 2026-09-16 on echo-prismatic PR #342 (T-3629 sccache wiring, artisan half): initial `gh pr merge 342 --squash --admin --delete-branch` returned the GraphQL error verbatim. Re-run ~30 min later (after Monitor fired on `mergeStateStatus == CLEAN`) succeeded at merge commit `51a957561`. Same pattern reported to artisan for future guarded-inert PRs (agent branches pushed --no-verify + immediate admin-merge attempts).

## Related

- `bugs/agent-nested-ampersand-bash-produces-untracked-phantom-background-not-a-wake-drop.md` (similar "GH-timing racy-window" surface class)
- Standing HO discipline: [[reference_echo_agent_branch_merge_flow]] (worker → HO push+PR+merge, HO-exclusive per T-1673) — the racy window is squarely on HO's side
