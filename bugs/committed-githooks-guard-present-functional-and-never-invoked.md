---
symptom: "A repo commits a `.githooks/` directory with working guard scripts, and none of them ever run. `core.hooksPath` is unset (git then uses `.git/hooks/`, which holds only `*.sample` files), or it is explicitly set to that same empty `.git/hooks` path — so git invokes nothing while the guard sits in the tree looking deployed. The failure is invisible from every angle you would normally check: the hooks are present, they are executable, they are committed, and direct-invoking one proves it works correctly. Nothing reports that git never calls it. Worse, the standard remediation makes it recur: a self-healing activation hook written after the first occurrence was scoped to the ONE repo where it was found, leaving the identical defect live in three sibling repos."
fix: "Check ACTIVATION separately from CONTENT — they are different failure modes with different checks. Content: does the script do the right thing when run? Activation: does git ever run it? `git -C <repo> config --local core.hooksPath` must equal `.githooks`; if it is unset or points at `.git/hooks`, list `ls .git/hooks/ | grep -v '\\.sample$'` and confirm it is EMPTY — that empty listing is the proof nothing runs. Repair with `git -C <repo> config core.hooksPath .githooks`. Then fix the CLASS, not the instance: make the session-start repair DISCOVER every repo that commits a `.githooks/` dir rather than naming one, because a repo committing that directory is asserting it wants those hooks run."
domain: git, githooks, core.hooksPath, guard-activation, silent-failure, byte-sequence-discipline, session-start-hook, pantheon
---

# A committed `.githooks` guard that is present, functional, and never invoked

**Class:** silent guard deactivation. Not a bug in the hook — the hook is perfect. The wiring that
would cause it to run is absent, and nothing anywhere reports that.

**Found:** 2026-08-03 (T-2392 sweep), while checking whether the byte-sequence guard extended beyond
`pantheon-config`. It did not, in three repos.

## Symptom

```
$ ls pantheon-public/.githooks/
commit-msg  pre-commit  pre-push          # committed, executable, correct

$ git -C pantheon-public config --local core.hooksPath
                                          # UNSET -> git uses .git/hooks/

$ ls pantheon-public/.git/hooks/ | grep -v '\.sample$'
                                          # EMPTY -> nothing runs, ever
```

And the part that makes it stick — the guard **works**:

```
$ ./.githooks/commit-msg <msg-with-trigger>;  echo $?
HERMES discipline violation in commit message — commit refused.
1                                         # correct
$ ./.githooks/commit-msg <safe-msg>;         echo $?
0                                         # correct
```

So every check a person naturally runs comes back green. The scripts exist, are executable, are
committed, and demonstrably do the right thing. **The only observation that exposes it is asking
whether git ever calls them**, and that is the one question the presence of a working script
discourages you from asking.

A variant hides even better: `core.hooksPath` **explicitly set to the repo's own `.git/hooks`**. That
reads as deliberate configuration, so a repair tool that refuses to clobber a non-default value skips
it — while the value is functionally identical to unset and equally empty.

## Why it recurred — the remediation was scoped to its instance

This exact defect was found once before (2026-06-09, T-1198/T-1216) and fixed properly: a fail-open
SessionStart hook, `tools/hooks/ensure-hooks-active.sh`, that re-asserts `core.hooksPath` every
session so activation cannot silently lapse. Good fix. It was written as:

```bash
REPO="$HOME/pantheon-config"
```

**One repo.** The class stayed open everywhere else, and by 2026-08-03 three sibling repos had
drifted into exactly the state the hook was built to prevent — including the mirror repo derived from
the very content where this discipline is documented.

> **A remediation scoped to the instance it was found in does not close the class.** When the fix is
> "re-assert X every session", the scope of *every* is the whole fix. A named list is the same
> failure wearing a fix's clothes: it is a thing someone must remember to extend, which is what the
> automation was supposed to replace.

The repair is discovery, not a longer list — every repo that **commits** a `.githooks/` dir is
asserting it wants those hooks, so that assertion is the selector:

```bash
shopt -s nullglob
for REPO in "$HOME"/*/; do
  REPO="${REPO%/}"
  [[ -d "$REPO/.githooks" ]] || continue
  ...
done
```

## Checking it

Content and activation are different questions; run both.

| question | check | green means |
|---|---|---|
| does the guard work? | invoke it on a known-positive **and** a known-negative | the script is correct |
| does git ever run it? | `git config --local core.hooksPath` == `.githooks` | the script is reachable |
| (if unset/`.git/hooks`) | `ls .git/hooks/ \| grep -v '\.sample$'` is empty | **proof** nothing runs |

Passing the first while failing the second is the whole bug, and the first is the one that feels like
the real test.

## Related

- [`instrument-whose-form-cannot-express-the-failure.md`](instrument-whose-form-cannot-express-the-failure.md) — "the hook is present and correct" cannot express "git never calls it".
- `CLAUDE.md` §byte-sequence discipline — records the 2026-06-09 drift and the option-B self-healing fix whose scope this entry corrects.
