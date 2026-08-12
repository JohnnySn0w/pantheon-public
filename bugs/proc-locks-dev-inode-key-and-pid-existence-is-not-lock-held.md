---
symptom: "A lease/liveness check built on a lockfile reports HELD when nothing is held (silently suppressing a detector), or NOT-HELD when something is (a reaper unlinks a live lock and forks it). Four distinct causes: (a) probing the pid recorded in the lockfile with os.kill(pid,0), which asks 'does a process exist' not 'is the lock held'; (b) matching the /proc/locks entry on the INODE alone, when the table is keyed by (device, inode) and spans every mounted filesystem; (c) keying on the path's CURRENT inode when the lock lives on a previous, now-unlinked one; (d) returning the same value for 'the kernel says nothing holds it' and 'the kernel could not be asked'."
fix: "Read /proc/locks and match the full key: (major, minor, inode), major/minor parsed as HEX, inode as decimal. Find the FLOCK/POSIX token by search, never by fixed index — a blocked waiter renders as '1: -> FLOCK ...' and shifts every column. Return a THREE-valued answer (held / not-held / cannot-tell) and let each caller pick its own safe direction: a detector fails OPEN, a kill path fails CLOSED. Never substitute a pid-existence probe."
domain: pantheon, mailbox, flock, systemd, liveness, /proc
---

# /proc/locks: keyed by (dev, inode) — and a pid in a lockfile is not a lock

## Symptom

A guard that means "is this lease currently held?" answers wrongly, silently, in
whichever direction the bug happens to point:

- Wrong HELD in `mail_staleness_monitor` suppresses staleness detection for the
  lease's whole TTL and emits nothing at all.
- Wrong NOT-HELD in `_lease._reap` unlinks a LIVE holder's lockfile; the retry
  `O_CREAT`s a second inode, flock is per-inode, and you now have two live leases
  on one logical lock with no signal anywhere.

Same predicate, opposite consequences. That asymmetry is the whole reason this
entry exists.

## Cause

Four separate traps, all reached from the same intent.

**(a) A pid is not a lock.** `fcntl.flock()` is released BY THE KERNEL on process
exit, but the lockfile's JSON record is just text and survives. So after any exit —
crash *or* routine restart — the record still names pid N while the kernel holds
nothing. `os.kill(N, 0)` then answers "a process with this pid exists", which after
pid recycling is a different question with the same shape. Note the frequency: the
stale record is the STEADY STATE between restarts, not a crash edge case, so the
wrong predicate gets many chances to be wrong.

**(b) An inode number is unique only WITHIN its filesystem.** `/proc/locks` is a
system-wide table spanning every mounted filesystem, so comparing `stat().st_ino`
against the entry's inode alone performs the lookup outside the namespace where the
key is unique. Any process flocking any file on any other filesystem with the same
inode number reports YOUR lease as held.

**(c) The lock lives on an inode, not on a path.** If the lockfile is unlinked and
recreated, the old inode's lock survives on the holder's open fd while the path now
resolves to a NEW inode. `stat(path)` gives you the wrong inode, and both locks are
simultaneously live and visible in /proc/locks.

**(d) "Cannot tell" is not "not held".** This is the one that bites AFTER you have
fixed the other three, and it is reached only through the error path. If the
function returns `None` for both "the kernel says nothing holds this" and "the
kernel could not be asked", every caller silently picks a default it never chose.
Two routes reach it, and the second needs no simulation at all:

1. `/proc/locks` unreadable → falls back to "not held" → a reaper unlinks a live
   holder's lockfile and forks the lock.
2. `path.stat()` swallowing `OSError` → **unlink the lockfile while its flock is
   still held and the module denies its own live lock.** Trap (c)'s unlink makes
   that state reachable in normal operation, so this is routine, not an edge.

`FileNotFoundError` is the exception: a missing lockfile IS a definitive answer
("nothing holds it"), and folding it into "cannot tell" makes an ordinary tombstone
un-reapable.

⭐ **ENUMERATE THE EXITS, NOT THE CASES.** This trap's most instructive instance was
a function that returned the RIGHT sentinel on one error path and the wrong one on
another, four lines apart, with the correct contract stated in its own docstring:

```python
def flock_held(lock) -> bool | None:
    """True = held, False = not held, None = cannot tell (caller should fail OPEN)."""
    try:    st = lock.stat()
    except OSError: return False    # <- conflated
    try:    text = Path("/proc/locks").read_text()
    except OSError: return None     # <- SAME distinction, correct, 4 lines later
```

The author was not missing the knowledge — they applied it correctly at one exit and
did not carry it to the other. A function that handles the class at ONE return
presents as having handled the class, most convincingly to its own author, who
remembers deciding it and does not re-derive it at the next return. **The docstring
makes it worse**: it documents the contract the code then breaks, so reading it is
exactly what makes the code look finished. Check every `return` against the stated
contract, not every scenario against your memory of having thought about it.

## Fix

```python
UNKNOWN = _Unknown()   # a sentinel whose __bool__ RAISES, so `if holder:` can
                       # never silently restore the meaning this removes

def flock_holder_pid(lock: Path) -> int | None | object:
    """int = held by that pid | None = definitively NOT held | UNKNOWN = blind."""
    try:
        st = lock.stat()
    except FileNotFoundError:
        return None                        # no lockfile at all -> definitively free
    except OSError:
        return UNKNOWN                     # stat failed for some OTHER reason -> blind
    try:
        text = Path("/proc/locks").read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN                     # cannot consult the kernel -> blind, NOT "free"
    want = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    for line in text.splitlines():
        parts = line.split()
        # "1: FLOCK  ADVISORY  WRITE 1729 fd:01:2889489 0 EOF"
        # A BLOCKED WAITER renders as "1: -> FLOCK ..." and shifts every field,
        # so find the token; a fixed index silently mis-reads.
        try:
            i = parts.index("FLOCK")
        except ValueError:
            continue
        if i + 4 >= len(parts):
            continue
        d = parts[i + 4].split(":")
        if len(d) != 3 or not d[2].isdigit():
            continue
        try:
            if (int(d[0], 16), int(d[1], 16), int(d[2])) == want:   # dev halves are HEX
                return int(parts[i + 3])
        except ValueError:
            continue
    return None
```

**Then choose the direction AT THE CALLER, because the consequence lives there:**

```python
# DETECTOR — unknown must not suppress. Suppression is affirmatively justified.
def flock_held(lock) -> bool | None:
    h = flock_holder_pid(lock)
    return None if h is UNKNOWN else h is not None

# KILL / UNLINK PATH — unknown must not act. A wrong answer kills a process
# group or forks a lock.
def _kernel_says_unlocked(lock) -> bool:
    return flock_holder_pid(lock) is None      # UNKNOWN -> False -> do nothing
```

⚠️ **Do NOT "consolidate" these by making both callers return the same type.** That
is the tempting reading of "one implementation" and it destroys the only thing that
makes consolidation safe. Share the PREDICATE, which hands back a distinguishable
unknown; keep the DIRECTION at each caller. A detector that fails closed goes blind;
a reaper that fails open kills things.

Field notes:
- **major and minor are HEX; the inode is DECIMAL.** `fd:01:2627193` is device
  253:1. Parsing `fd` as decimal raises and the match silently never fires — a
  quiet failure in the opposite direction.
- **A corpse cannot appear in this table.** The entry is dropped when the fd
  closes, so presence *is* holding; no liveness probe is needed on top.
- **Use `POSIX`/`OFDLCK` instead of `FLOCK`** if that is what your lock actually is;
  the token you search for must match the locking call you made.
- **One implementation per repo.** This predicate produced FOUR shipped bugs in a
  single night while it existed as two independent copies serving opposite
  consequences. If you import the shared one across module boundaries, import it
  through a single owner module — two import paths (`tools._lease` vs `_lease`)
  produce two distinct `UNKNOWN` singletons, and `holder is UNKNOWN` then silently
  reads every unknown as a definite answer.

## How to validate it (this is where all the real bugs got through)

Take a REAL flock rather than mocking the kernel — but mock the `/proc/locks` TEXT
for the device case, because a real cross-device inode collision is not reliably
reproducible:

- known-positive: hold a real flock → held.
- known-negative: `LOCK_UN` → not held, immediately.
- **the money case**: write a record naming `os.getpid()` (live BY CONSTRUCTION)
  with no lock held → must be NOT held. A pid-existence predicate says held here.
- device case: feed a row with the same inode on a different major → must be NOT held.
- blindness case: make `/proc/locks` unreadable while a real flock IS held → must be
  UNKNOWN, and the reaper must refuse to act. Run the SAME harness with procfs
  readable as a CONTROL, so the result is shown to discriminate rather than merely
  agreeing with your hypothesis: procfs up → 1 live lease, procfs down → 2.
- mutation-verify: restore each broken predicate and confirm the matching case goes
  red. **Print the mutated line** — a mutation that silently fails to apply reads
  exactly like a passing verification.

⚠️ **A real cross-device collision is constructible but NOT durably reproducible.**
Two separate tmpfs mounts (e.g. `/dev/shm` and `/run/user/$UID`) have independent
inode counters, so creating ~3000 files in each yields many shared inode numbers —
one run found 2361. But tmpfs allocates from a MONOTONIC counter and never reuses,
so the two mounts only collide while their counters are in step; an hour later the
same harness found ZERO. Do not build a regression test on it — it is green-by-drift.
Mock the table text and fix the device in the test instead of reading the host's.

⚠️ **Beware a precondition guard that cannot fail.** The first attempt at the hex
regression test contained
`self.assertTrue(any(c.isalpha() for c in f"{maj:x}{mn:02x}") or True)` — `X or True`
is always True, so it never checked the precondition it was written for. Without it
the test only discriminates hex from decimal when the host's tmpdir device
cooperates; the dead zone is **major <= 9 AND minor <= 9**, which is exactly `/tmp`
on tmpfs (systemd default on several distros, normal in containers). Fix the device
in the test with `os.makedev(0xfd, 0x01)` and assert both directions.

⚠️ **A test on an IMPOSSIBLE premise asserts a bug as expected behaviour.** One of
these traps was hidden for a night by a test in which a dead process held a kernel
flock — a state that cannot exist. If the fixture cannot reach the state, the
assertion is about nothing; the mutation check above is what catches it.

## Discovered

- T-2291 (trap a) → fixed `713d4365d`, 2026-07-29
- T-2296 (trap b) → fixed `c0ebbca86`, 2026-07-29
- T-2292 (trap c) → the unlink/reap path that produces two live leases on two inodes
- T-2307 (trap d) → fixed `ab7ca74f8`, 2026-07-29
- T-2304 → the two copies consolidated to one predicate, `ae2a5b086`
- Traps (a)–(c) found by specialist; (d) found by specialist in the fix for (a)–(c). Fixed by
  recovery. Four bugs in the SAME predicate inside one night, each one found inside the
  fix for the previous — **the first correction is evidence the area is subtle, not
  evidence it is now clean.**
- The "enumerate the exits" note above is specialist correcting recovery's write-up of this
  entry: recovery had excused the drafted snippet as written before trap (d) was known.
  It was not — the distinction was already in the same function, four lines below the
  break. Worth keeping as the record, because "the author lacked the knowledge" makes
  the defect unpreventable while "the author applied it at one exit only" makes it
  checkable.

## Keywords

/proc/locks, flock, fcntl, LOCK_EX, lease, lockfile, stale lock, os.kill pid 0,
pid existence, pid recycling, inode collision, st_dev, st_ino, os.major, os.minor,
makedev, device major minor hex, blocked waiter, consumer.lock, staleness monitor,
silent suppression, fail open, fail closed, cannot tell, tristate, UNKNOWN sentinel,
tmpfs inode counter, forked lock, double import trap
