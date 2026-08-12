---
symptom: "A lock-holder check against /proc/locks returns 0 hits for every candidate and the zero is WRONG BY CONSTRUCTION — the query never could have matched. /proc/locks renders the DEVICE halves in hex and the INODE in DECIMAL (`fd:01:2889489` is major 0xfd, minor 0x01, inode 2889489), so a check that formats the inode as hex — the natural move once you know the device halves are hex — matches nothing and reports every lock as free. Two sibling shapes fail the same way: parsing the dev:ino triple by COLUMN INDEX (line field-count varies; blocked waiters render as `->` continuations, POSIX and FLOCK rows differ), and keying on the inode ALONE (an inode is unique only within one filesystem, so an ino-only match against a system-wide table can return a false HELD — T-2296)."
fix: "Parse by TOKEN SHAPE, not column index: scan each line's fields for one matching `^([0-9a-f]+):([0-9a-f]+):(\\d+)$` and read it as (major_hex, minor_hex, inode_DECIMAL). Key on the full (dev_major, dev_minor, inode) triple from `os.stat` — `os.major(st.st_dev)`, `os.minor(st.st_dev)`, `st.st_ino` — never the inode alone. AND PROVE THE INSTRUMENT BEFORE BELIEVING ANY ZERO: create a real lock (`f=open(p,'w'); fcntl.flock(f, fcntl.LOCK_EX)`), assert the parser finds it, and print that verdict BEFORE the answer. A /proc/locks query cannot fail loudly — a wrong format yields a plausible, quiet, well-typed zero, so a clean result is a claim about the QUERY unless a known-positive has been run in the same invocation."
domain: "proc-locks, flock, lease, mailbox, instrument-validation, known-positive, linux, pantheon"
---

# `/proc/locks`: hex device halves, DECIMAL inode — and a blind check returns a plausible zero

**Class:** measurement integrity. Not a kernel bug — the documented rendering, in a format whose two
numeric conventions differ within a single field.

**Found:** 2026-08-02 (T-2409, checking whether two paused seats' MCP servers were holding mailbox
consumer leases).

## The shape

```
1: FLOCK  ADVISORY  WRITE 1729 fd:01:2889489 0 EOF
```

`fd:01:2889489` is **major `0xfd`, minor `0x01`, inode `2889489` decimal**. The mixed convention is
the whole trap: once you learn the device halves are hex — which is the part people write down —
formatting the inode as hex is the *consistent-looking* next step, and it is wrong.

## Why it is worse than an ordinary off-by-one

**The failure mode is a quiet, well-typed, plausible zero.** There is no error, no warning, no
exception — the grep simply matches nothing and every candidate reports NOT HELD. That is
indistinguishable from the true answer whenever the true answer is also "nothing is held", which is
most of the time.

In the instance that found it, **the blind zero agreed with the answer eventually confirmed.** Had
the known-positive not been run, the wrong method would have shipped a right answer and taught
nothing — waiting for the run where "nothing is held" is the wrong answer. A bad instrument that
happens to be correct does not announce itself; it certifies itself.

## The three ways to get it wrong, all seen

1. **Inode formatted as hex.** Matches nothing. Reports everything free.
2. **Parsed by column index.** `/proc/locks` lines vary in field count — blocked waiters render as
   `->` continuations, and POSIX vs FLOCK rows differ — so `fields[4]` is not always the dev:ino
   triple. Raises `ValueError: not enough values to unpack` at best, silently reads the wrong field
   at worst.
3. **Keyed on the inode alone.** An inode is unique only *within one filesystem*. Matching an
   ino-only value against a system-wide table can return a false **HELD** and silently suppress a
   detector — T-2296, found inside the T-2291 fix.

Note (1) and (3) fail in **opposite directions**: one manufactures freedom, the other manufactures
occupancy. A checker with both bugs can be wrong either way depending on the corpus.

## The rule this generalises to

`/proc/locks` is kernel-owned and a corpse cannot appear in it — when an fd closes the entry is
dropped — so **presence IS holding**, and no liveness probe is needed. That makes it an unusually
trustworthy source, which is exactly why a blind query against it is so convincing.

> **A zero is a claim about the world only if the query is known-good. Otherwise it is a claim about
> the query.**

So the known-positive is not optional hygiene here; it is what converts the output into evidence.
Print its verdict *before* the answer, so a reader cannot see the answer without seeing whether the
instrument worked.

## See also

- `lessons/2026-07-23-proxy-vs-defining-surface-synthesis.md` — Face 6 (dead guard) and its
  through-line: *could this test have come out otherwise?* A query that cannot match is that
  question's cheapest instance.
- `bugs/instrument-whose-form-cannot-express-the-failure.md` — the sibling class.
- T-2296 — the (dev,ino) vs ino-alone false-HELD.
