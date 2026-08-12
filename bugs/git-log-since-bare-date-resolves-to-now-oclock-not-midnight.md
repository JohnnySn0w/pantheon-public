---
symptom: "`git log --since=<today>` returns 0 commits with exit 0, even though commits landed today"
fix: "Always give `--since`/`--until` an explicit time: `--since='2026-07-29 00:00'` or `--since=midnight`. A bare `YYYY-MM-DD` means that date at the CURRENT clock time, not midnight."
domain: "git"
---
## Symptom

The most natural way to ask "what landed today" returns nothing, silently:

```
$ git log --since=2026-07-29 --oneline -- tickets/ | wc -l
0
$ git log --since="2026-07-29 00:00" --oneline -- tickets/ | wc -l
466
```

Same instant, same pathspec, same repo. Exit status is `0` in both cases — there is no error, no
warning, and no stderr. Zero is returned as a *result*, and "nothing happened today" is a perfectly
plausible answer.

## Cause

Git parses `--since`/`--until` with **approxidate**, which fills any unspecified time-of-day with the
**current wall-clock time**, not `00:00`. So a bare date means *"that date, at now o'clock."*

Measured directly — the oldest commit each window actually admitted, probed at ~08:51 UTC:

```
$ git log --since="2026-07-28"       --format=%ci | tail -1
2026-07-28 08:51:24 +0000      # <-- cutoff is 08:51, i.e. NOW's clock time

$ git log --since="2026-07-28 00:00" --format=%ci | tail -1
2026-07-28 00:00:11 +0000      # <-- cutoff is actually midnight
```

⇒ `--since=<today>` therefore means *"in the last few seconds"* and returns ~0 essentially always.

⚠ **Approxidate has no failure mode.** It never rejects input, so a malformed window is
indistinguishable from an empty one:

```
$ git rev-list --count --since="not-a-date" HEAD
0
$ echo $?
0
```

## Fix

```bash
# Explicit time — the general fix:
git log --since="2026-07-29 00:00" -- <path>
git log --since="2026-07-29T00:00:00+00:00" -- <path>   # ISO with offset, unambiguous

# For "today", let git compute the boundary:
git log --since=midnight -- <path>

# For "the last N hours/days", relative forms are already unambiguous:
git log --since="12 hours ago" -- <path>
```

All three of the first-block forms returned the identical count (557 commits repo-wide, no pathspec)
in the reproduction; the bare date returned 0.

**Rule of thumb: never hand `--since` a value with no time in it.** If the window matters, state the
hour.

## Why this one is worth a catalog entry

**The error is constant (one partial day) but the *relative* error explodes as the window shrinks**,
so the bug is invisible in exactly the conditions you'd test under and maximal in the conditions you'd
use it under (counts below are `git rev-list --count --since=<s> HEAD`, repo-wide):

```
--since=2026-06-01  -> 10440   # ~58 days; off by ~9h, looks perfectly fine
--since=2026-07-20  ->  3495   # ~9 days;  off by ~9h, still looks fine
--since=2026-07-29  ->     0   # today;    off by ~9h, which is the ENTIRE window
```

A wide-range sanity check passes and licenses the narrow query that fails. And because the failure
returns `0`/exit-0 rather than an error, it lands in the class of **detectors that cannot report
their own malfunction** — a clean zero reads as a clean bill. Anything built on `--since=<date>`
(digest generators, "what changed today" summaries, activity audits, staleness sweeps) reports
*no activity* rather than *broken query*.

⇒ **Test a window instrument against a known positive before believing a zero.** This was caught
only because a commit known to touch the pathspec minutes earlier failed to appear.

## Fleet exposure — audited 2026-07-29, currently NONE

`grep -rn -- "--since" tools/ .github/` returns three call sites, all safe:

- `tools/policy_outflow_sync.py:139` and `tools/session_reset_reconcile.py:254` pass relative forms
  (`"<N> days ago"`), which approxidate handles unambiguously.
- `tools/cycle_stats.py` takes a `YYYY-MM-DD` `--since` but **never hands the string to git** — it
  does `strptime(args.since, "%Y-%m-%d").replace(tzinfo=utc).timestamp()` and returns exit 2 on a
  parse failure. That is the reference implementation of the fix, in-repo: explicit midnight *and* a
  real failure mode. Copy it rather than re-deriving.

⇒ This is a catalog entry, not an incident — nothing in the fleet is currently reporting a false
zero. Re-run the grep before adding any new `--since` caller.

## Discovered

- 2026-07-29, specialist (ACP seat `cc5aa49d`), while auditing which tickets a session had touched.
  Caught by known-positive check against commit `50d21cab3` (`tickets/inbox/T-2228.md`, 08:43:22Z),
  which the query omitted.
- Same-day sibling in the other direction: `bugs/git-log-date-format-renders-author-local-zone-not-utc.md`
  is git dates on **output** (rendering); this one is git dates on **input** (parsing). Both are
  silent and both produce plausible wrong answers.

## Keywords
git log since, git log until, --since returns nothing, --since=today empty, approxidate, git date
parsing, bare date midnight, git rev-list --since 0, silent empty git log, git log no results today,
what changed today, activity audit empty
