---
symptom: "a before/after guard (snapshot at setUp, diff at tearDown) reports a 'new' item that was there all along, e.g. 'Real repo gained new local-only commits', naming a commit no test made. It happens only under load, and the failing test is not the culprit."
fix: "represent a failed snapshot read as UNKNOWN (None), never as an empty set; when either side is unknown, skip the comparison with a warning. Keep fail-lenient helpers (return frozenset() on error) out of any code path used as a BASELINE."
domain: "pantheon, testing, fixtures, ci, test-isolation, contention, false-positive"
---

## Symptom

On 2026-09-28, pantheon-config PR CI runs (#495, #497, #501) failed `assert_real_repo_untouched` with "Real repo at /opt/actions-runner-pc/_work/... gained new local-only commits". Each run was caught by a different test (test_session_reset_reconcile, test_ticket TestMainCheckoutGuard, test_ticket_lint). Every run was under heavy runner IO contention (T-4084).

## Cause

The guard snapshots `git log HEAD --not --remotes=origin` at setUp and diffs it at tearDown. The shared helper `local_only_commits` is **fail-lenient by contract**: on timeout (15s) or error it returns `frozenset()`. Under IO starvation the setUp read timed out, so the baseline became EMPTY. At tearDown the read succeeded and returned the checkout's HEAD. On a `pull_request` run that is GitHub's `refs/pull/N/merge` commit, which is local-only by construction. So the diff "found" a commit that had been there the whole time.

Lenient on one side plus successful on the other gives a false positive. "Couldn't look" and "looked, saw nothing" were the same value.

Found by adding forensic detail to the assertion (T-4108 step 1): the "leaked" commit's subject was `Merge <head> into <base>`, which is the PR merge ref, not anything a test wrote.

## Fix

`tools/_test_fixtures.py` (T-4108, PR #501): the guard's own snapshots use `_local_only_commits_or_none`, which returns None on failure, with a 60s timeout. `assert_real_repo_untouched` skips with a `RuntimeWarning` when either side is None. The lenient `local_only_commits` stays as-is for callers that want it.

**General rule:** any before/after comparison needs a three-valued read (value / empty / unknown). A helper that maps failure to "empty" is fine for display and dangerous as a baseline.
