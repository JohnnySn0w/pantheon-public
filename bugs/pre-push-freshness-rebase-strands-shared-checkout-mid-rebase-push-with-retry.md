---
symptom: "The shared pantheon-config checkout is left MID-REBASE after a ticket op: `git status` shows `UU tickets/README.md` (or a ledger/appetite shard) and `.git/rebase-merge` exists; the ticket.py op printed 'git push failed' and did not land, and the next ticket op refuses on the dirty tree"
fix: "Since T-4380 (merge dc04c84e5) push_with_retry detects a rebase that was absent at entry but present after a failed push (the pre-push hook's freshness rebase), resolves it with the caller's conflict resolver and retries, or runs `rebase --abort` with a LOUD message naming the unpushed commits. If you hit the symptom on an older clone: keep upstream's README rows, drop the resolved ticket's stale active row (or union the appetite shard lines), `git add`, `git rebase --continue`, then push."
domain: pantheon, git, pre-push hook, ticket.py, push_with_retry, rebase, shared checkout
---

# The pre-push freshness rebase strands the shared checkout mid-rebase on a generated-file conflict

**First observed:** 2026-09-30 05:17Z (T-4380). A resolve commit's push ran `.githooks/pre-push`, whose freshness step (`git pull --rebase --autostash` on an attached branch) stopped on a `tickets/README.md` conflict.

## Symptom

```
## main...origin/main [ahead 1, behind N]
UU tickets/README.md
$ ls .git/rebase-merge   # present
```
The ticket.py op reported `git push failed` and did not land. The next ticket op fails its pre-write pull on the unmerged tree.

## Cause

On an ATTACHED branch the pre-push hook rebases onto origin when the clone is behind, and on a conflict it exits 1 WITHOUT aborting, leaving `.git/rebase-merge`. Its stderr carries none of the non-fast-forward markers (`rejected`, `non-fast-forward`, `fetch first`), so `tools/_git_utils.py` `push_with_retry` classed the failure as non-retryable and returned. The T-0846 `_board_conflict_resolver`, which exists to regenerate exactly this generated board, was never consulted. The hook is deliberately unchanged (T-4380 Q1): its human-facing contract is to stop and let a person resolve.

## Fix

In `push_with_retry` (T-4380):
- **Only a rebase the push created is touched.** Rebase state is probed at entry (`_rebase_in_progress_kind`, which reads `git rev-parse --git-path rebase-merge|rebase-apply`, so it is worktree-correct) and only for a checkout root (`_is_checkout_root`, which checks `cwd/.git` only; the empty `/tmp/.git` decoy on this VPS is why there is no parent walk). A rebase already in progress at entry is never touched.
- **Resolved:** after a failed push, if the rebase is present and a `conflict_resolver` is given with an attempt left, it resolves, runs `rebase --continue`, and retries the push.
- **Otherwise aborted, loudly:** it runs `rebase --abort` and returns `(False, msg)`. The message names each unpushed commit (`git log --oneline origin/<dst>..HEAD`) and the manual recovery. If the abort itself fails, the message says the checkout is STILL MID-REBASE.
- **A rebase present at entry is untouched on EVERY path.** That includes the pre-existing non-fast-forward path, which before T-4380 fetched, ran `git rebase FETCH_HEAD`, drove the caller's resolver over the person's own conflict, and could abort their rebase. It now returns `(False, msg)` naming the in-progress rebase. The op is still attempted first; refusing to commit at all while mid-rebase is T-4422.
- **Appetite shards are unioned, pure appends only:** `_board_conflict_resolver` also resolves `ledger/appetite/*.jsonl` conflicts by `git merge-file --union` (upstream lines first), deduping only the lines the union added. The shards are append-only event logs, so they cannot be regenerated. `_is_pure_append` makes it decline unless the base is a per-line prefix of both sides. An edit or rewrite of history on either side, or a delete/modify, declines and the rebase aborts. Any other conflicted path still declines.

Sibling callers that run a raw `git push` and meet the same hook (delivery_rebase_controller, pantheon_config_sync, session_reset_reconcile) are T-4388.

## Verification

Real git, no mocks: a bare origin plus two clones with `core.hooksPath` set to the repo's real `.githooks`, and a conflicting `tickets/README.md` pushed from the second clone. Then:
- on the pre-fix base, `push_with_retry` returns non-retryable and `.git/rebase-merge` remains;
- on the fixed code, the push lands after the resolver runs, or the rebase is aborted with the unpushed commit named.

Tests: `tools/test_git_utils_push_retry.py` (TestPushWithRetryHookStrandedRebase) and `tools/test_ticket_push_rebase_conflict.py`. 12/12 implementer mutants were killed; the reviewer's M6/M9/M11 plus 7 of 11 new mutants were killed (the 4 survivors are test-strength only; tracked on T-4422).

## Discovered

- T-4380 (root cause from the 05:17:28Z reflog, 2026-09-30); fix merged dc04c84e5 (#613).
- Stranding seen repeatedly on 2026-09-30 during a dense merge train (the HANDOFF gotcha "keep upstream's rows, drop the resolved ticket's active row").

## Keywords

rebase-merge, stranded rebase, mid-rebase, UU tickets/README.md, pre-push hook, freshness rebase, git pull --rebase --autostash, push_with_retry, non-retryable, _board_conflict_resolver, conflict_resolver, rebase --abort, ledger/appetite, jsonl union, merge-file --union, ticket.py resolve, git push failed, shared checkout, T-4380, T-0846, T-4388
