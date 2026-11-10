# Unknown must not stand in for no

**Authored** 2026-11-02 by Orchestrator (Orchestrator) per **T-4559** (sweep 2026-10-04 item B1), adopted from
mason's draft. Promotes a rule that until now lived only in Recovery's private pin
`feedback_unknown_must_not_stand_in_for_no` (cited by name in `bugs/` and tickets that other seats couldn't
follow). This is **Face 12** of `lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md`.

**Re-evaluate when:** a repo-wide lint or typed helper makes the three-valued read structural (then demote
this to a pointer at it); or an instance appears where mapping unknown to a definite answer was provably
correct for EVERY caller (that would argue for a narrower rule).

---

## The claim

A probe that **could not look** and a probe that **looked and saw nothing** must not return the same value.
When "couldn't ask" collapses into "no", "empty", "absent", "idle", "down" or "0", every caller inherits a
confident answer that was never measured, and acts on it in whichever direction that caller treats as safe.

## Origin (2026-07-29): one night, four instances in the lease/lock layer

The rule was found in a single night of lease and lock fixes, which is what Recovery's pin recorded:

- **T-2291:** "a process with that pid exists" (`os.kill(pid, 0)`) stood in for "it holds the lease".
- **T-2296:** an inode match without the device let a FOREIGN lock stand in for ours.
- **T-2292:** the lockfile's text stood in for the kernel's actual lock state.
- **T-2307 (canonical, destructive):** an unreadable `/proc/locks` stood in for "unlocked".
  `_lease.flock_holder_pid` returned `None` for "could not find out", and `_reap` treated `None` as
  permission to unlink, which FORKED the lock. The fix made three outcomes explicit: int (held by that pid),
  None (definitively not held), UNKNOWN (could not find out); `_reap` now refuses to act while blind. (The fix
  note also landed on T-2305 by an ID collision; T-2307 is its home.)

## Later instances (9, across five components)

| # | Instance | "Couldn't look" became | Consequence |
|---|---|---|---|
| 1 | `bugs/guard-baseline-snapshot-timeout-treated-as-empty-false-positive.md` (T-4108) | a fail-lenient `frozenset()` | a before/after guard reported a commit no test made, under load only |
| 2 | `bugs/proc-locks-dev-inode-key-and-pid-existence-is-not-lock-held.md` (trap d) | "pid exists" read as lock-held or not-held | the SAME predicate suppressed a detector one way and reaped a live lease the other |
| 3 | `bugs/shell-sentinel-masks-exit-code-blind-predicate.md` (T-2359) | `cmd \|\| echo MISSING` | a blind read reported a confident CAUSE ("bridge died"); the `rc != 0` guard was unreachable |
| 4-7 | `bugs/sudo-u-to-runners-own-account-fails-per-seat-cross-account-tool-false-null.md` (4 recurrences) | a sudo-to-self denial with empty stdout | a healthy own seat read as MISSING, "no recent write", or "cannot verify" |
| 8 | T-4070 (`pantheon_metrics_gen`) | `_svc_show` returning `''` on a failed cross-account probe | service "down" and agent "idle" on the canonical fleet panel (ORG-098) |
| 9 | T-4236 (memex `records.sensitive DEFAULT 0`) | never-classified read as non-sensitive | fail-OPEN on a privacy flag |

## Why it isn't False GREEN (the splitting test)

False GREEN's remedy is "find the defining signal and read THAT". Here the right signal IS being read; the
defect is what the reader does when the read FAILS. Reading a better surface can't fix it: the fix is in the
TYPE of the answer. So it splits into its own face.

## Where it comes from: the most natural line in Python

`except OSError: return None` is the most natural line in Python, and it silently merges two facts: "there is
nothing" and "I could not find out". Reading the function's return type tells you nothing. You have to ask
what each CALLER does on the negative. The same `None` can be harmless or destructive:
`mail_staleness_monitor._flock_held` returning `None` on a failed read was safe because its caller fails open
(it just doesn't suppress an alert), while `_lease.flock_holder_pid` returning `None` led `_reap` to unlink a
live lock (T-2307).

## The remedy

0. **Sweep in priority order: start with callers that SIGNAL, DELETE or UNLOCK on the negative.** There the
   conflation is destructive (a killed process, a removed file, a forked lock), not merely a wrong number on
   a panel. Recovery's original audit (T-2309) used this order.
1. **Three-valued read.** Every probe returns value / empty (looked, nothing there) / UNKNOWN (could not
   look), as distinct values: `None` vs `frozenset()`, a named `UNKNOWN` sentinel, an enum, or `(ok, value)`.
   A helper that maps failure to "empty" is fine for DISPLAY and dangerous as a BASELINE or a gate.
2. **Each caller picks its own fail direction, explicitly.** The same UNKNOWN must fail closed in a guard
   (refuse), fail quiet in a staleness detector (don't alert on "can't tell"), and render as "unknown" on a
   panel. The probe must not pre-decide that by choosing which definite value to fake.
3. **Name sentinels as don't-know words, never as conclusions.** UNKNOWN, CANNOT-TELL or DIRTY-OR-FAILED
   force the caller to branch; MISSING, DEAD, ABSENT or 0 silently become categories
   (`bugs/shell-sentinel-masks-exit-code-blind-predicate.md`).
4. **A DEFAULT is a value too.** A column or field default (`DEFAULT 0`, `False`, `[]`) that means "never
   set" must not equal a real answer. Use NULL or an explicit "unclassified".
5. **Test the unknown path.** For every probe, a test where the read FAILS asserts that the caller sees
   UNKNOWN, not the safe-looking default.

## Cross-references

- Face 1 (False GREEN) and Face 6 (Dead guard) in the family map are the near neighbours.
- `lessons/2026-07-28-failure-presents-as-absence.md` and its 2026-11-02 generalisation (T-4558) are the
  sibling face: there a mechanism keyed on a moved target fails open as SILENCE; here a probe that can't ask
  fails as a confident NO.
- `lessons/2026-07-22-pipeline-exit-code-masks-command-failure.md`: the shell mechanism behind several of the
  instances above.

**Authoring discipline:** hand-curated per D2. HO authors `lessons/` per ORG-012.
