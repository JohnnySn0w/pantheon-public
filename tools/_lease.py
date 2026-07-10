"""tools/_lease.py — single-holder, stale-safe, daemon-safe file lease (T-0896).

Factors the proven handoff_and_restart.sh T-0584 "Stale-flock takeover" pattern
into a reusable Python primitive. Built for the Veneration harness (T-0766) — only
one veneration may run against the shared the-ancestor substrate at a time — but it
is path-agnostic for any single-holder coordination need.

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
        rec = {"pid": os.getpid(), "start": int(time.time()), "label": self.label}
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
        if isinstance(start, int) and (now - start) > self.ttl_secs:
            return True  # wedged-but-alive past TTL
        return False

    def _reap(self, rec: dict) -> None:
        pid = rec.get("pid")
        if isinstance(pid, int) and self._pid_alive(pid):
            self._signal(pid, signal.SIGTERM)
            deadline = time.time() + self.grace_secs
            while time.time() < deadline and self._pid_alive(pid):
                time.sleep(0.05)
            if self._pid_alive(pid):
                self._signal(pid, signal.SIGKILL)
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
