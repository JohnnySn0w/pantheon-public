"""tools/_lease.py — single-holder, stale-safe, daemon-safe file lease (T-0896).

Factors the proven handoff_and_restart.sh T-0584 "Stale-flock takeover" pattern
into a reusable Python primitive. Built for the Veneration harness (T-0766; removed
T-4741 #835) — only one veneration could run against the shared (since-retired)
the-ancestor substrate at a time — and it is path-agnostic for any single-holder coordination need.

Properties (per T-0766 design B/C/D):

- DAEMON-SAFE: fcntl.flock(LOCK_EX | LOCK_NB) — the lock is owned by the open file
  description and released IMPLICITLY by the kernel on process exit. A holder that
  crashes / is OOM-killed / SIGKILLed frees the lease automatically; there is no
  manual-unlock step that a dead process could skip. A daemon can therefore
  request the lease without risking a permanent wedge if its child dies.

- NON-BLOCKING: acquire() never blocks. On contention by a live, non-stale holder
  it returns False so the caller backs off rather than stalling its poll cycle.

- STALE-SAFE (defense-in-depth for a wedged-but-alive holder that won't release):
  a contended acquirer reads the holder record and, if the holder PID is dead OR
  has held past ttl_secs, reaps it (SIGTERM → grace → SIGKILL) and removes the
  lockfile, then retries ONCE. Mirrors T-0584 Layer 2 (the implicit-release Layer
  1 covers the common case; this covers the zombie-holder edge).

- OBSERVABLE: the lockfile carries a JSON record {pid, start, label} so a human or
  a reaper can see who holds the lease and why.

Open/lock are guarded separately: a permission/path error on the lockfile must not
be misreported as lock contention (the 2026-05-21 cross-user /tmp footgun).
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import time
from pathlib import Path

DEFAULT_TTL_SECS = 900  # 15 min — a resume + single HANDOFF write is minutes (T-0766 (E)).
_CONTENDED_ERRNOS = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK)


class _Unknown:
    """Sentinel: the kernel's lock state could not be determined (T-2307).

    Distinct from None ("definitively not held") because callers act on the
    two oppositely — see Lease.flock_holder_pid.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "UNKNOWN"

    def __bool__(self) -> bool:
        # Never let `if holder:` silently read as "not held".
        raise TypeError("UNKNOWN lock state must be handled explicitly, not coerced to bool")


UNKNOWN = _Unknown()


class LeaseHeld(Exception):
    """Raised by acquire(raising=True) / __enter__ when held by a live, non-stale holder."""


class Lease:
    def __init__(self, path, *, ttl_secs: int = DEFAULT_TTL_SECS, label: str = "",
                 grace_secs: float = 2.0):
        self.path = Path(path)
        self.ttl_secs = int(ttl_secs)
        self.label = str(label)
        self.grace_secs = float(grace_secs)
        self._fd: int | None = None

    # ------------------------------------------------------------------ record
    def read_record(self) -> dict | None:
        """Current holder record, or None if absent/empty/malformed (fail-open)."""
        try:
            data = self.path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not data:
            return None
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def _write_record(self) -> None:
        # ttl_secs is RECORDED because staleness is a property of the HOLDER, not of
        # whoever happens to ask (T-2236, archivist). Without it, `_holder_is_stale`
        # judges a long-lived holder against the ACQUIRER's TTL: an MCP consume
        # (DEFAULT_TTL_SECS=900) declared a 7200s ACP bridge stale after 15 minutes
        # and SIGKILLed it — same uid, so the kill was permitted. Measured live with
        # both bridges past threshold: specialist 1365s, archivist 2708s.
        rec = {
            "pid": os.getpid(),
            "start": int(time.time()),
            "label": self.label,
            "ttl_secs": self.ttl_secs,
        }
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, (json.dumps(rec) + "\n").encode("utf-8"))
        os.fsync(self._fd)

    # ---------------------------------------------------------------- staleness
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not ours → treat as alive (don't reap blindly)
        except (OverflowError, ValueError):
            return False  # garbage/out-of-range pid in the record → not a live holder
        return True

    def _holder_is_stale(self, rec: dict, now: int) -> bool:
        pid = rec.get("pid")
        if not isinstance(pid, int):
            return True  # malformed record → reapable
        if not self._pid_alive(pid):
            return True  # dead holder, lockfile orphaned
        start = rec.get("start")
        # Honour the HOLDER's recorded TTL; fall back to ours only when absent
        # (a record written before T-2236, or by an older tool on this host).
        # Reading self.ttl_secs here is what let a 900s acquirer reap a 7200s
        # holder — the acquirer does not know what the holder was promised.
        holder_ttl = rec.get("ttl_secs")
        if not isinstance(holder_ttl, int) or isinstance(holder_ttl, bool) or holder_ttl <= 0:
            holder_ttl = self.ttl_secs
        if isinstance(start, int) and (now - start) > holder_ttl:
            return True  # wedged-but-alive past the holder's OWN TTL
        return False

    def flock_holder_pid(self) -> int | None | object:
        """PID actually holding the flock on this lockfile, per the KERNEL (T-2292).

        THREE outcomes, and conflating two of them forked the lock (T-2307, specialist):
            int        -> held by exactly that pid
            None       -> definitively NOT held
            UNKNOWN    -> we could not find out (procfs unreadable, stat failed)

        The first version returned None for both "not held" and "cannot tell".
        _reap acts on those OPPOSITELY: `holder is None` took the
        clear-the-record branch, so an unreadable /proc/locks made a live
        holder lose its lockfile, the retry O_CREATed a second inode, and the
        lock forked — the exact outcome the T-2292 commit existed to remove,
        reachable again through the error path. specialist reproduced it with two
        real processes and a control: procfs up -> 1 live lease; procfs down
        -> 2.

        Keyed on MAJOR:MINOR:INODE —
        all three, because an inode number is unique only within its filesystem
        while /proc/locks spans every mounted one (T-2296, proven by a real
        cross-tmpfs collision). major/minor are HEX there; the inode decimal.

        This is the ONLY trustworthy answer to "who holds this lease". The
        lockfile's own `pid` field is text somebody wrote down: it survives the
        writer's death, and Linux recycles pids.
        """
        try:
            st = self.path.stat()
            ino, maj, minr = st.st_ino, os.major(st.st_dev), os.minor(st.st_dev)
        except FileNotFoundError:
            return None          # no lockfile at all -> definitively not held
        except OSError:
            return UNKNOWN       # stat failed for some OTHER reason -> blind
        try:
            text = Path("/proc/locks").read_text(encoding="utf-8")
        except OSError:
            return UNKNOWN       # cannot consult the kernel -> blind, NOT "free"
        for line in text.splitlines():
            parts = line.split()
            # A blocked waiter renders as "1: -> FLOCK ...", shifting every
            # field, so locate the token rather than trusting a fixed index.
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
                if (int(d[0], 16), int(d[1], 16), int(d[2])) == (maj, minr, ino):
                    return int(parts[i + 3])
            except ValueError:
                continue
        return None

    def _reap(self, rec: dict) -> None:
        """Evict a stale holder. NEVER signals a pid that does not hold the lock.

        T-2292 (specialist). This previously signalled `rec["pid"]` — a field the
        LOCKFILE CONTAINS — guarded only by "is some process with that pid
        alive". Two ways that is wrong, and _signal escalates to os.killpg, so
        the blast radius is a whole PROCESS GROUP:

          RECYCLED PID: holder dies, its record survives naming pid N, Linux
          hands N to something unrelated -> we SIGTERM then SIGKILL an innocent
          process group. Same defect class as T-2291, except that one only
          suppressed a detector and this one kills.

          SURVIVING HOLDER: the unlink was UNCONDITIONAL, so a holder that
          ignored the signals lost its lockfile anyway. The next acquire
          O_CREATs a NEW INODE, and flock is per-inode -> TWO LIVE LEASES on
          one logical lock. Mutual exclusion is the entire product of this
          module, so that outcome is worse than a failed acquire.
        """
        pid = rec.get("pid")
        holder = self.flock_holder_pid()

        if holder is UNKNOWN:
            # T-2307: blind. Signalling risks an innocent group; unlinking
            # risks forking a lock the kernel is still enforcing. Do NEITHER
            # and let the acquire fail — recoverable and loud, which is the
            # correct direction when mutual exclusion is the product.
            return

        if holder is not None and pid != holder:
            # T-2311 (specialist): the kernel POSITIVELY names a live holder, and it
            # is not the pid in the record text. Unlinking here is the
            # SURVIVING-HOLDER case this docstring calls worse than a failed
            # acquire — reached through a DEFINITE answer, so the T-2307 UNKNOWN
            # split does not cover it.
            #
            # Production route needs no error at all, only acquire()'s own
            # ordering (flock succeeds, THEN _write_record): A crashes leaving
            # text=A; B flocks; C contends, reads A, finds A dead so stale,
            # reaps, sees holder=B, B != A, and unlinks B's lockfile. C's retry
            # O_CREATs a new inode and flock is per-inode -> two live leases.
            #
            # STALE TEXT IS NOT A REASON TO DISTURB A LIVE LOCK. Touch nothing;
            # the acquire fails, which is recoverable and loud.
            return

        if holder is None:
            # Nothing holds the lock — the record is a tombstone. Clear it so
            # the retry can proceed, and send NO SIGNAL: we have no evidence
            # this pid has anything to do with this lease. Orphan reapability
            # lives here and is unaffected by the branch above.
            try:
                self.path.unlink()
            except (FileNotFoundError, OSError):
                pass
            return

        self._signal(pid, signal.SIGTERM)
        deadline = time.time() + self.grace_secs
        while time.time() < deadline and self.flock_holder_pid() == pid:
            time.sleep(0.05)
        if self.flock_holder_pid() == pid:
            self._signal(pid, signal.SIGKILL)

        # Only remove the record once the KERNEL agrees the lock is gone.
        # Surviving holder -> leave it; acquire fails, which is the correct
        # outcome and is recoverable. Unlinking here forks the lock instead.
        if self.flock_holder_pid() is None:
            try:
                self.path.unlink()
            except (FileNotFoundError, OSError):
                pass

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        # Signal the holder's whole process group when it leads one, else the pid.
        # Best-effort: a dead/foreign target is a no-op, never an error.
        try:
            pgid = os.getpgid(pid)
            if pgid == pid:  # holder is a group leader → take its children too
                os.killpg(pgid, sig)
                return
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    # ------------------------------------------------------------------ acquire
    def acquire(self, *, reap_stale: bool = True, raising: bool = False) -> bool:
        """Non-blocking acquire. True on success; False if held by a live, non-stale
        holder. If reap_stale and the holder is stale (dead or past TTL), reap it and
        retry ONCE. raising=True raises LeaseHeld instead of returning False."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            # Guard the OPEN separately from the LOCK so a perm/path error is not
            # misreported as contention.
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o660)
            except OSError as exc:
                raise OSError(
                    f"lease: cannot open lockfile {self.path} "
                    f"(permission/path, not contention): {exc}"
                ) from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno not in _CONTENDED_ERRNOS:
                    raise
                # Contended. On the first attempt, reap a stale holder and retry.
                if attempt == 1 and reap_stale:
                    rec = self.read_record()
                    if rec is not None and self._holder_is_stale(rec, int(time.time())):
                        self._reap(rec)
                        continue
                if raising:
                    raise LeaseHeld(f"lease {self.path} held: {self.read_record()}")
                return False
            # Acquired.
            self._fd = fd
            self._write_record()
            return True
        if raising:
            raise LeaseHeld(f"lease {self.path} held: {self.read_record()}")
        return False

    def release(self) -> None:
        """Release the lease (idempotent). The kernel also releases on process exit,
        so this is best-effort cleanup, not the correctness mechanism."""
        if self._fd is None:
            return
        try:
            os.ftruncate(self._fd, 0)  # clear the holder record
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        finally:
            self._fd = None
        # Deliberately do NOT unlink on release: leaving the (now-empty) lockfile
        # avoids an unlink/reopen race with a concurrent acquirer; the empty file
        # is harmless and the next acquirer reuses it.

    def held(self) -> bool:
        return self._fd is not None

    # --------------------------------------------------------------- ctx manager
    def __enter__(self) -> "Lease":
        self.acquire(raising=True)
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
