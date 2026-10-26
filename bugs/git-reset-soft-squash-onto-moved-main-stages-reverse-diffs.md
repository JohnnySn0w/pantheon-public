---
symptom: "squashing a WIP branch with `git reset --soft origin/main` after main moved stages other people's commits as REVERSE diffs (their new files show as D, their edits undone)"
fix: "squash onto the new main with `git reset --hard origin/main && git cherry-pick --no-commit <wip>` (or reset --soft to the merge-base), never --soft straight onto a main that has moved"
domain: "git"
---
## Symptom
You squash a WIP branch before handing it off. After `git fetch`:

```
$ git reset --soft origin/main
$ git status --short
A  mine.txt          <- your change
D  other.txt         <- someone else's file that landed on main after you branched
M  tickets/active/T-1234.md   <- someone else's ticket edit, reversed
```

`git diff --cached --stat` lists files you never touched. Committing this silently reverts every commit that landed on main since your branch point.

## Cause
`reset --soft X` moves HEAD to X but keeps YOUR tree as the index. The staged diff is therefore "X → your tree". Your tree predates the commits that moved main, so they show up as reversed. It only matches "just my changes" when X is your branch's merge-base, not a main that has moved since.

## Fix
Squash onto the CURRENT main by replaying only your own diff:

```bash
git fetch origin
wip=$(git rev-parse HEAD)                   # your WIP commit(s) tip
git reset --hard origin/main
git cherry-pick --no-commit <base>..$wip    # or a single <wip> commit
git diff --cached --stat                    # must list ONLY your files
git commit -F msg.txt
```

(Alternative: `git reset --soft $(git merge-base HEAD origin/main)`, commit, then rebase onto origin/main.)

Before re-trusting any test results, check main didn't touch your files meanwhile: `git diff --stat <tested-base> origin/main -- <your files>` should be empty.

Don't use `git stash` as the parking step in a shared clone. The stash list is repo-wide across worktrees (see `git-stash-is-repo-wide-not-worktree-scoped-cross-arc-collision.md`).

## Verification
Re-runnable repro, about 2s, in a throwaway dir. It prints `A  mine.txt` AND `D  other.txt`:

```bash
d=$(mktemp -d) && cd $d && git init -q -b main origin && cd origin && git -c user.email=a@b -c user.name=a commit -q --allow-empty -m base && echo a>a.txt && git add a.txt && git -c user.email=a@b -c user.name=a commit -q -m a && cd .. && git clone -q origin work && cd work && git checkout -q -b feat && echo mine>mine.txt && git add mine.txt && git -c user.email=a@b -c user.name=a commit -q -m WIP && cd ../origin && echo other>other.txt && git add other.txt && git -c user.email=a@b -c user.name=a commit -q -m other && cd ../work && git fetch -q && git reset -q --soft origin/main && git status --short
```

Observed live twice on 2026-09-29 (recovery, T-4276 and T-4284 squashes). One case staged a 135-line deletion of another agent's knowledge/episodic README plus reversed ticket edits. `git diff --cached --stat` caught it before commit.

## Discovered
- 2026-09-29, recovery, while squashing agent/T-4276 and agent/T-4284 for handoff (fix applied: cherry-pick onto fresh main; landed a7f04b263 / 6590d7aa0).

## Keywords
git reset --soft, squash, WIP, squash onto main, moved main, reverse diff, reverted someone else's commit, unexpected deletions staged, D in git status, cherry-pick --no-commit, merge-base, handoff branch, PR prep
