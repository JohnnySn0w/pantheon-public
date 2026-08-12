---
symptom: "A repo path everyone treats as canonical (`~/<repo>`) is a DETACHED clone that can neither `git pull` (\"You are not currently on a branch\") nor reattach (\"'main' is already used by worktree at ...\"), because a side worktree holds its branch. It drifts silently — measured at 165 commits behind within a day — while looking like a normal clone from every angle. Fleet sweeps that `cd` there get confident WRONG answers: a cross-account sweep read a seat as still-failing after the fix had landed, because it measured 165-commit-old code and the stale tree answered."
fix: "Do NOT hold the primary branch in a side worktree. Keep the canonical clone ON the branch and give throwaway work its own per-arc worktrees. Detect with: `git worktree list` — if the primary path shows `(detached HEAD)` while another worktree holds the branch, it is stranded and cannot self-heal. Repair: confirm the side worktree is clean and unpushed-empty, `git worktree remove` it, then `git checkout <branch> && git pull` in the canonical clone. Do NOT 'fix' this with a README marker saying 'not the working tree' — a marker is a proxy for a gate, and a sweep that `cd`s there does not read markers. LIFECYCLE COROLLARY (two polarities): ask not only 'does this control still work?' but 'is the hazard it compensates for still real?' AND 'is this mitigation still delivering what I credit it for?' — the second is harder to see because the control still functions, just degraded, so citing it stays superficially true."
domain: "git, worktree, detached-head, fleet-sweeps, measurement-integrity, stale-checkout, pantheon"
---

# A worktree holding the primary branch strands the canonical clone detached, and it cannot self-heal

**Class:** measurement integrity via the filesystem. Not a git bug — the documented behaviour of
`git worktree`, in a layout that makes a canonical path silently non-canonical.

**Found:** 2026-07-29 (recovery measured it on the atlas seat; atlas caused it and fixed it, T-2340).

## The trap, and why it is self-inflicted by a reasonable fix

The layout is usually created to solve a real collision: an implementer agent checks out a feature
branch **in the shared clone**, changing the branch under the orchestrator's own running commands.
Giving the orchestrator a side worktree pinned to `main` fixes that — and creates this:

```
~/pantheon-config     DETACHED HEAD, 165 commits behind, clean
                      git pull     -> "You are not currently on a branch"
                      git checkout main -> "'main' is already used by worktree at ~/wt-atlas-main"
~/wt-atlas-main       on main, clean, pullable — the REAL tree
```

A worktree holding `main` means the primary clone **cannot check out `main`**, so it sits detached;
a detached clone **cannot pull**; so it drifts, and nothing about it looks wrong.

## Why it is not merely untidy

**It produces confident wrong measurements for other people.** A cross-account sweep did
`cd ~/pantheon-config` and reported that seat as still failing a test that a merged fix had already
repaired. The sweep was correct about what it measured and wrong about what it claimed, because the
canonical path was no longer canonical and **the stale tree answers**. Every sweep assuming
`~/<repo>` is a seat's live tree has the same exposure, and nothing looks broken.

This is [a proxy reporting in the thing's voice](../lessons/2026-07-29-proxy-reported-in-the-things-voice.md)
arriving through the filesystem rather than through code.

## Detection

```
git worktree list
```
If the primary path shows `(detached HEAD)` while a *different* worktree holds the branch, it is
stranded. Confirm with `git -C <primary> log --oneline HEAD..origin/main | wc -l`.

## Repair

1. Confirm the side worktree is safe to release — `git status --porcelain` empty **and**
   `git log origin/<branch>..HEAD` empty.
2. `git worktree remove <side-worktree>`
3. In the canonical clone: `git checkout <branch> && git pull --ff-only`
4. Verify from the *consumer's* angle, not your own: `HEAD == origin/<branch>`, and grep the tree for
   a change you know landed recently.

Give throwaway work its own per-arc worktrees instead. Note those accumulate — a sweeper keyed to a
`/tmp/` prefix will not touch `~/wt-*`, so that cleanup stays manual.

## Do not fix it with a marker file

The tempting cheap option is to leave the clone detached and drop a `README` at its root saying
"not the working tree; see `~/wt-…`". **A marker is a proxy for a gate.** The sweep that caused the
wrong measurement did `cd <path> && <command>`; nothing in it reads markers, and nothing would have.
Same failure as trying to stop a ticket being picked up by saying so in a mailbox message instead of
setting `depends_on` — prose does not gate anything that is not reading prose. Fix the path.

## The general form, which is the durable half

**When a workaround's original hazard gets fixed properly, the workaround does not become free — it
becomes UNOWNED.**

The shared-clone collision that motivated the side worktree was genuinely solved a better way:
implementer agents began creating their own per-arc worktrees unprompted. At that moment the side
worktree stopped being load-bearing — and *kept standing*, with nobody's attention on it, quietly
producing wrong answers for a third party who had no idea it existed.

Compensating controls are usually reviewed against "does it still work?", which this passed
perfectly. The question that would have caught it is **"is the thing this was compensating for still
true?"** — and nothing routinely asks that. It generalises past worktrees to every compensating
control: a retry wrapper around a fixed flake, a cache warming a query that got fast, a lock around
a race that was designed out. Each keeps working, and each keeps costing.

### The question has two polarities, and the second one is harder to see

The form above asks *is the hazard still real?* — the control outlived its reason. There is an
inverse, and it is the more common shape because the control is still present and still functioning:

> **Is this mitigation still delivering what I credit it for?**

Found within hours of the above being written, by orchestrator turning the question on his own work
(T-2325). He had downgraded a byte-sequence guard gap on the reasoning that the LOCAL hook is blind
to oddly-named staged files but the SERVER-side audit catches them — a hole in one layer, not in the
guard. Measured afterwards: the oldest queued audit run was created at 08:09Z and still queued at
10:03Z, with the queue filling faster than it drains, so the lag grows.

**The downgrade was true when written and expired because an unrelated change moved a precondition
it never named.** The second layer still exists, still works, and still catches the case — eventually.
Nothing about it *stopped*; it got slower, and "the server-side audit catches this" stayed
superficially true while ceasing to justify the downgrade that cited it.

That is why this polarity is harder: the first form leaves a control doing nothing, which at least
shows up as cost. The second leaves a control doing *something*, which reads as coverage. Add it to
the list — **a second layer credited for coverage it is no longer delivering promptly**. Each keeps
working; each keeps costing, or keeps being cited.

### And a third disposition: some claims should not be in the artifact at all

Both polarities above assume the claim belongs there and asks whether it is still true. There is a
case where the answer is neither — **do not assert a status whose update cadence you do not
control.**

Found by orchestrator after his handoff row about another seat's live status was wrong **twice in one day,
in both directions** — first "seat closed" while that seat was mid-arc, then "active, in rebound"
an hour after it closed. Both true when written. Both false within the hour. His fix was not another
register row; it was to stop asserting another seat's live status in a file he updates on his own
cadence, and to mark the bullet itself as expirable.

That is the sharper move, because a register row makes a claim *checkable* while leaving the
mismatch in place: the fact changes on someone else's schedule and the file changes on yours, so the
window between them is permanent and only its size varies. A row converts silent rot into a chore.
Removing the claim removes the rot.

**The test:** for each status assertion in a durable artifact, ask *who changes the underlying fact,
and on whose schedule?* If the answer is not you, prefer a pointer to the source over a copy of its
current value — `see tickets/active/` beats "T-2334 is active", and the pointer cannot go stale.
Where the copy genuinely earns its place (an offline reader, a boot-time summary), it needs the
expiry treatment: what invalidates it, and the one command to re-check.

Note this also bounds the register idea itself. A register lists claim *types*; a per-seat status
buried in a prose bullet is not a type, so it read as covered while being uncovered — the
"listing fewer claims than exist reads as complete" hazard, arriving one level below the list.

## Related

- `lessons/2026-07-29-proxy-reported-in-the-things-voice.md` — the class this is an instance of.
- `bugs/guard-compares-loaded-value-against-re-derivation-sharing-its-fallback.md` — same shape in
  config loading rather than in the filesystem.
