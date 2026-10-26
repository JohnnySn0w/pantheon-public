---
symptom: "In a test that creates a bare origin via `git init --bare`, then pushes 'main' from a working clone, downstream `git clone` from that bare falls back onto a fresh empty local 'master' branch. A later `git push origin HEAD:main` from a peer clone then fails with `! [rejected] HEAD -> main (non-fast-forward)` — because it's pushing a root-commit-only-master onto main. On this host (init.defaultBranch unset), 14 delivery_rebase_controller tests failed silently via that push."
fix: "After the initial `git push -u origin main`, add one call: `git -C <bare-origin> symbolic-ref HEAD refs/heads/main`. The bare's HEAD then points at main; every downstream clone lands on main; peer pushes fast-forward. Fix pattern is at test-fixture level (setUp), not per-clone."
domain: "git, pytest, hermetic-test-fixtures, bare-repos, git-clone, init.defaultBranch, non-fast-forward"
---

## Symptom

```
$ git init --bare origin.git       # this bare's HEAD → refs/heads/master (default)
$ git init work && cd work
$ ... commit + branch -M main ...
$ git remote add origin ../origin.git
$ git push -u origin main          # creates refs/heads/main on bare, HEAD symref untouched

$ cd .. && git clone origin.git peer
Cloning into 'peer'...
warning: remote HEAD refers to nonexistent ref, unable to checkout
done.
$ cd peer                          # now on a fresh empty local 'master'
$ echo x > x && git add -A && git commit -m x
$ git push origin HEAD:main
 ! [rejected]        HEAD -> main (non-fast-forward)
error: failed to push some refs to '/.../origin.git'
```

The peer's HEAD is on `master`, not `main`. When it tries `HEAD:main`, git treats it as "push my master to remote main" — and since local master has no ancestry to remote main, the push is rejected non-fast-forward.

Fatal for hermetic tests that use `_git(..., check=True)` — the exception bubbles up as CalledProcessError, and the test appears to fail on push rather than surface the underlying HEAD-symref state.

## Cause

`git init --bare` sets HEAD to a symbolic reference at `refs/heads/<init.defaultBranch>` (or `refs/heads/master` when unset). Pushing `main` from a working clone creates `refs/heads/main` on the bare **without** updating its HEAD symref. Downstream clones look at the bare's HEAD, can't resolve it (`refs/heads/master` doesn't exist there), print `warning: remote HEAD refers to nonexistent ref, unable to checkout`, and fall back to creating a fresh empty local `master` branch.

This bites hosts where the operator's git has `init.defaultBranch` unset (system default of `master`) — most Linux distros as of 2026. If the developer's own git has `init.defaultBranch=main` locally, the tests pass on their box and fail on CI or a peer machine.

## Fix

One line at test-fixture setUp, after the initial push:

```python
_git("push", "-u", "origin", "main", cwd=self.repo)
_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)   # this
```

Now the bare's HEAD points at main. Every downstream clone lands on main; peer pushes fast-forward. Covers ALL clones from the bare (in-loop, in-helper, in-later-tests).

Alternative fixes considered and rejected:
- **`checkout main` after every clone** — duplicative, fragile against new test additions, easy to forget one.
- **Set `init.defaultBranch=main` in git config** — affects host state; not portable to CI runners where the setting is unset.
- **Use `git branch -M main` in the bare** — bare repos don't have a working tree; `branch -M` is a working-tree operation. `symbolic-ref` is the correct primitive.

## Verification

Repro (one-liner):
```bash
cd /tmp && rm -rf t && mkdir t && cd t && git init --bare o.git && git init w && cd w && git config user.email t@t && git config user.name t && echo x > x && git add -A && git commit -m x && git branch -M main && git remote add origin ../o.git && git push -u origin main && cd .. && git clone o.git p && cd p && echo y > y && git add -A && git commit -m y && git push origin HEAD:main
```
Expected before fix: `! [rejected]        HEAD -> main (non-fast-forward)`.

Apply the fix (`git -C ../o.git symbolic-ref HEAD refs/heads/main` after the first push), re-run the last three commands, expected: `To ../o.git\n   xxxxxx..yyyyyy  HEAD -> main`.

Confirmed on this host 2026-09-16 during T-3753 investigation (14 delivery_rebase_controller tests collapsed to this one root cause).

## Discovered

- T-3753 investigation, commit `dbd4019fb` (tools/test_delivery_rebase_controller.py setUp), 2026-09-16.
- Sibling: T-3752 diagnostic report at `knowledge/episodic/2026-09-16-t3752-pytest-xdist-flakiness-diagnostic/report.md` mistakenly attributed the 22 failures to xdist race; 14 of them collapsed to this single bare-origin-HEAD bug once isolated.

## Keywords

git init --bare, HEAD symref, refs/heads/master, refs/heads/main, non-fast-forward, HEAD:main, remote HEAD refers to nonexistent ref unable to checkout, git symbolic-ref, init.defaultBranch, hermetic test fixture, temp bare origin, peer clone falls back to master, push -u origin main, tmp git test, git integration test, CalledProcessError push origin
