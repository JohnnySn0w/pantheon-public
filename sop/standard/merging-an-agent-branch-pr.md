# SOP: Merging an agent-branch PR (pantheon-config, echo-prismatic, pantheon-pwa)

**Status:** Adopted 2026-09-30 by Orchestrator (HO) under msg-3733 principles-as-ratification (below-goal HO-practice methodology; ORG-012: HO authors SOPs). Filed under T-4465 so it can render as an auto-triggering skill. It consolidates the HO memory pins that carried this procedure (echo merge flow, REST PR create, drain-before-merge, stacked-PR squash, merge train, never-chain-delete), corrected against the live repos by a 2026-09-30 accuracy review. The procedure ran ~25 times on 2026-09-30 alone. The operator override-available.

## When to use this

You hold an `agent/<ticket>` branch that a reviewer APPROVED (or a peer orchestrator asked you to merge), and it has to land on `main` of pantheon-config, echo-prismatic or pantheon-pwa. PR and merge are **HO-exclusive**. Who pushes depends on the author. Subagents (implementers, reviewers, in-session project-orchestrators) never push: the worker-guard denies it (T-1673), so the HO pushes their branch. A peer-orchestrator **seat** (artisan, mason) is not a subagent and pushes its own `agent/<ticket>` branch (retask-dispatch, T-1922/T-1929), then hands off with an MR mail. Either way the HO opens the PR and merges (T-4549).

## Procedure

1. **Push the branch.** Plain `git push -u origin agent/<ticket>` from a worktree on that branch. The pre-push hook runs; let it. **No hook bypass.** `rulebook/policies/trust-posture.md` "What is never permitted" lists "`--no-verify` or any hook bypass"; the HO reads that as covering pushes too (2026-09-30 correction, after artisan declined a `--no-verify` push on T-4463). CI re-running the smoke is not a carve-out. If the hook is slow, run the push in the background. (echo's pre-push hook still prints a `--no-verify` hint; ignore it.)
2. **Open the PR with REST, not `gh pr create`** (GraphQL deprecation errors and 500s):
   `gh api -X POST repos/example-org/<repo>/pulls -f title="…" -f head=agent/<ticket> -f base=main -F body=@<file> --jq .number`.
   The body ends with the Claude Code attribution line. Before retrying a create that 5xx'd, check whether the PR already exists (`pulls?head=example-org:<branch>&state=all`).
3. **Find out what gates this PR, then poll it with a script file in the background.** The PAT cannot read check-runs or `statusCheckRollup` (403 / silently empty), so poll workflow runs.
   - **pantheon-config:** CI is path-filtered and informational. The `protect-main` ruleset has no required status checks. If the diff touches none of `ci.yml`'s `on.pull_request.paths` (e.g. a docs/sop/ticket/ledger-only PR), there is NO CI run: don't wait for one. Otherwise, poll `gh run list --branch <b> --workflow CI --json databaseId,headSha`, match the head sha, and wait for `completed`/`success`. Always read the `HERMES discipline audit` run, which fires on every push and PR.
   - **echo-prismatic:** `ci-gate` is the required check (with `enforce_admins`). Poll the run's **jobs** (`gh run view <id> --json jobs`) and key on `ci-gate`. Heavy jobs serialize on the runners, so run-level `status` can read `queued` long after jobs finish. Fail fast on any required job failing; `windows-check` is advisory.
   - **pantheon-pwa:** there is **no CI**. Verification is manual, by the reviewer or you, on the branch:
     `bun install --frozen-lockfile && bun run typecheck && bun test`, then
     `cd web && bun install && bunx svelte-kit sync && bun run check && bunx vitest run` (`svelte-kit sync` is REQUIRED before `check`). Compare the `check` error count against a clean `origin/main` baseline in the same environment, because missing Playwright types can add phantom errors.
   - Don't pipe a background command through `tail`/`grep`/`jq` at launch (the hook blocks it, and it eats the exit code). Write raw output and filter at read time.
4. **A red CI job that is not the change:** read the failed job's log (`gh api repos/example-org/<repo>/actions/jobs/<job-id>/logs`). If the failure is in code the PR doesn't touch and matches a timing/flake shape, re-run it, and file a flake ticket if none exists:
   - pantheon-config: `gh run rerun <run-id> --failed` is fine.
   - echo: **never `gh run rerun --failed`** (it re-runs heavy advisory jobs). Wait until the run completes, then re-run the single job: `gh api -X POST repos/example-org/echo-prismatic/actions/jobs/<job-id>/rerun`. Its dependents (`ci-gate`) re-run with it. Mid-run re-runs are silently ignored.
5. **Base freshness.** If the branch is far behind main and main has touched files the branch touches, or the code is adjacent to active work, merge `origin/main` into the branch in a temporary worktree, push, and let CI run on the merged head before merging. A textually clean merge-tree is not proof of a semantically clean merge.
6. **Immediately before merging, in the same turn:**
   - `mailbox_consume`, and scan for a HOLD or correction on this PR (a peer's HOLD can sit undelivered while CI runs);
   - `git fetch` and `git merge-tree --write-tree origin/main origin/agent/<ticket>`, which must be clean. Use `git -C <abs path>`: parallel Bash calls share one cwd;
   - confirm the PR head sha equals the sha that was reviewed/requested.
7. **Merge:** `gh pr merge <N> --repo example-org/<repo> --admin --squash --match-head-commit <full sha>`. Then read the state back: `gh api repos/example-org/<repo>/pulls/<N> --jq '"merged=\(.merged) \(.merge_commit_sha)"'`. On pantheon-config `--admin` is the ruleset bypass (PR + review + code-owner). On echo it doesn't bypass `ci-gate`, which must be green.
8. **Clean up only after `merged=true`:** remove worktrees (`git worktree remove … --force`), delete the local branch, delete the remote branch. Never chain the delete after the merge in one command: a failed merge still runs the delete.
9. **Close the loop:** resolve the ticket (`ticket.py resolve <T> --with "<merge sha + evidence>"`; it must be `active`, WIP cap 1, and it can refuse on the ORG-065 closure gate), or mail the peer author that it merged and is theirs to resolve.

## Stacks and deep queues

- **Stacked pair:** squash-merging the base leaves the top conflicting wherever it edits the base's hunks. Prefer merging the base and then `git merge origin/main` into the top before its CI run. If the top is already green, its PR merge ref (`refs/pull/<N>/merge`) holds the tested tree: `git commit-tree <that tree> -p <top head> -p <new main>`, verify the diff, push normally.
- **Echo, 3+ reviewed branches queued:** use a merge train. One `agent/merge-train-<date>` branch from `origin/main`, `git merge --no-ff` each branch in order, one PR, one ci-gate run, merged with `--merge` (not squash) so per-ticket commits survive. Cancel the superseded individual runs.

## Common failure modes

- Merging on green while the author had mailed a HOLD (echo #386, 2026-09-29). Step 6 exists for this.
- Waiting on a pantheon-config CI run that will never start because the diff is outside `ci.yml`'s paths.
- `gh pr edit … && gh pr merge …`: the edit fails on GraphQL, and the merge silently never runs. Always read state back.
- A push during a GitHub incident can leave the PR head stale (branch moved, PR didn't; CI never fires). Compare `pulls/<N>.head.sha` with the branch ref; close and reopen via REST to resync.
- `gh run view --json attempt` does not exist.
- Deleting a branch before the merge confirmed.

## Boundary

- This is the merge path only. Deploying what merged is a separate step: `deploying-the-live-echo-daemon.md` for echo; kino auto-deploys from pantheon-pwa `main` (`redeploy-pantheon-pwa.md`).
- HO seat only for PR and merge. Other seats push their own `agent/<ticket>` branch and hand it to the HO with an MR mail that names the branch, head sha, gate evidence and merge order. Subagents never push.

## Cross-references

- `staging-a-worker-dispatch.md`: how the branch got here (implementer and reviewer staging).
- `rulebook/policies/trust-posture.md`: the never-permitted list.
- CLAUDE.md §Agent branch convention (commit and push flow).
