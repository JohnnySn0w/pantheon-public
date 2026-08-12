---
symptom: "echo PR fails CI on a /home/claude absolute path in .cargo/config.toml that nobody deliberately edited"
fix: "stage files explicitly by path in a staged echo worktree; never git add -A — the staging script pre-dirties a TRACKED file"
domain: "echo-prismatic"
---
## Symptom

An echo change is clean, reviewed, and green locally, but CI rejects the PR for a
`/home/claude/...` absolute path in `.cargo/config.toml` — a file the author never
knowingly touched.

```
$ git -C /tmp/echo-T-XXXX status --short
 M .cargo/config.toml
?? auth-smoke
```

## Cause

Two independently correct things that combine into a trap:

1. `pantheon-config/tools/stage-echo-worktree.sh` appends a shared-lane target-dir to
   `.cargo/config.toml` so concurrent lanes share a cargo cache:

   ```toml
   [build]
   target-dir = "/home/claude/.cache/echo-lane-target"
   ```

   `.cargo/config.toml` is **tracked** (`git ls-files --error-unmatch .cargo/config.toml`
   succeeds), so the worktree is born with a staged-able modification to a real repo file.
   The change is *correct locally* — the build needs it — and *wrong in the repo*.

2. **T-1906** (merged as `adcefaf`, the newest commit on echo `main` at time of writing)
   added a CI check that rejects `/home/claude` absolute paths in workflow-reachable files.
   `.cargo/config.toml` is squarely one.

So `git add -A` / `git add .` / `git commit -a` in any staged echo worktree sweeps a
machine-specific absolute path into the branch, and the guard turns it into a red PR —
after review, on a change that was otherwise fine. The guard is new enough that avoiding
it is not yet anyone's habit, and nothing in the worktree warns you.

**Severity — verified, and it is worse than a red run.** The path guard lives inside
`.github/workflows/ci.yml`, and `ci-gate` (the single REQUIRED status check on `main` under
branch protection) aggregates that workflow's jobs. `ci-gate` is **not admin-bypassable**. So
committing the file does not merely produce a noisy failure — it **hard-blocks the merge**
and cannot be forced through with `--admin`. The only route out is a new commit removing the
path, i.e. a wasted review-and-CI cycle (~12 min per CI run) on an otherwise-approved change.

The untracked `auth-smoke` wrapper the same script drops in the worktree root is a milder
version of the same thing: harmless if committed, but it does not belong in the repo.

**And a bigger one, same root cause (verified 2026-07-23):** echo's `.gitignore` contains
`target/`, which does **not** match sibling-named build dirs. Two appear in a staged
worktree and neither is ignored:

| path | origin | size | ignored? |
|---|---|---|---|
| `target-wip/` | a lane build with its own `CARGO_TARGET_DIR` | **231 MB** observed | **no** |
| `target-auth/` | the generated `auth-smoke` wrapper (`CARGO_TARGET_DIR="${WT}/target-auth"`) | hundreds of MB | **no** |

Verify with `git check-ignore -v target-wip/` — exit **1** means NOT ignored (0 means it is).
Do not pipe that check into `head`/`grep` and test `$?`; the pipe returns the filter's status
and reports "ignored" for everything. (That mistake was made while investigating this very
entry — see `pipe-through-tail-head-swallows-exit-code.md`.)

The sharp edge is that `auth-smoke` is the wrapper *required* for the APPROVE-gating smoke, so
using the tooling correctly is what creates the second unignored directory.

## Fix

**In a staged echo worktree, stage explicitly by path. Never `git add -A`.**

```bash
git add crates/foo/src/bar.rs crates/foo/tests/baz.rs   # explicit paths only
git diff --cached --name-only                            # confirm before committing
```

`.cargo/config.toml` must not appear in that list. If it already got staged, unstage it
but keep the working-tree edit — the build depends on it:

```bash
git restore --staged .cargo/config.toml
```

**Brief this in the dispatch.** An implementer handed a pre-dirtied worktree has no way to
know the dirt was not theirs. Naming it in the brief costs a sentence; finding it in CI
costs a review cycle.

**Structural fix — LANDED (T-1986, 2026-07-23).** `stage-echo-worktree.sh` now makes a
freshly staged worktree `git add -A`-safe by construction, so the explicit-staging rule
above is a belt, not the only line of defence. The fix is NOT the originally-proposed
`CARGO_TARGET_DIR`-env switch — that would lose the shared-lane target-dir for bare `cargo`
commands (dev loops would each grow their own `target/`, reintroducing the T-1668 disk
concern). Instead:
- The tracked `.cargo/config.toml` still gets the `[build] target-dir` append (so bare
  cargo keeps using the shared lane), but the script then runs
  `git update-index --skip-worktree .cargo/config.toml` — the local edit becomes invisible
  to git (`git status` clean, `git add -A` won't stage it) while cargo reads it normally.
- `auth-smoke`, `target-auth/`, and `target-wip/` are appended to the repo's `info/exclude`
  (idempotent) so `git add -A` skips the untracked scratch too.

Verified on a throwaway stage: clean `git status`, `git add -A` stages nothing, target-dir
present + skip-worktree-flagged, vendor symlink + `ORT_DYLIB_PATH` intact.

## Discovered

- 2026-07-23, caught by Orchestrator (HO) inspecting a freshly staged `/tmp/echo-T-1493` worktree
  before the implementer's first commit — pre-emptively, not from a red PR.
- The two ingredients: `tools/stage-echo-worktree.sh` (shared-lane target-dir) and T-1906
  (`adcefaf`, the `/home/claude` path guard). Neither is wrong alone.
- Sibling: `bugs/echo-shared-target-dir-concurrent-lane-contention.md` covers the *other*
  consequence of the same shared target-dir (concurrent lanes blocking on the cargo lock).

## Keywords

.cargo/config.toml modified, dirty worktree, tracked file, git add -A, git commit -a,
/home/claude absolute path, CI path guard, T-1906, stage-echo-worktree.sh, target-dir,
CARGO_TARGET_DIR, auth-smoke untracked, echo worktree pre-dirtied
