"""Concurrency-safe read-modify-write for shared JSON config (T-2329).

WHY THIS EXISTS. `settings.json` is edited by fleet rollouts, and a seat can have
two executors (T-2274) that both act on the same mailed instruction. Two
read-modify-write cycles interleaving on one JSON file is a SILENT LOST UPDATE:
no error, no traceback, and no diff to notice — the loser's hook simply is not
there. Measured on the mnemosyne seat during the T-2313 rollout, where the two
executors raced and only an idempotence check (not the design) prevented it.

The existing idiom (e.g. `ci_audit_liveness._write_json`) does tmp-file +
`os.replace`, which makes the WRITE atomic — a reader never sees a half-file. It does not make
the READ-MODIFY-WRITE atomic, which is the lost-update case. This module adds the
missing half and keeps the atomic replace.

    from json_safe_edit import edit_json

    with edit_json(Path.home() / ".claude/settings.json") as cfg:
        hooks = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])
        if not any("my-guard.py" in h.get("command", "")
                   for e in hooks for h in e.get("hooks", [])):
            hooks.append(entry)

Three design choices, each of which a second implementation would plausibly get
wrong:

1. THE LOCK IS A SIDECAR (`<path>.lock`), NOT THE FILE ITSELF. `os.replace()`
   swaps the inode, and flock is per-inode — so a lock held on the config file is
   silently released-in-effect the moment the first writer replaces it, and a
   second writer acquires a lock on the NEW inode while the first still "holds"
   the old one. Two live locks, one logical resource. That is exactly the
   `_lease._reap` defect measured on T-2292 (unlink → `O_CREAT` retry → second
   flock succeeds on a fresh inode), reached here by a different route. The
   sidecar is never replaced, so it is a stable lock object.

2. THE WHOLE READ-MODIFY-WRITE IS INSIDE THE LOCK. Locking only the write moves
   the race rather than removing it: both writers would still have read the same
   pre-state and the second would still clobber the first.

3. IT WAITS; IT NEVER REAPS. `_lease.Lease` is the org's other lock and is
   deliberately NOT reused here — its `reap_stale=True` default SIGKILLs a holder
   it judges stale (and `killpg`s its group). That is defensible for a mail drain
   whose holder may be wedged; it is wrong for a config edit, where the other
   writer is a peer doing the same legitimate work and the correct behaviour is to
   wait a moment. A timeout raises rather than escalating.

4. BOTH FILES ARE `fchmod`'d, AND FOR THE SAME REASON — the config in `edit_json`
   and the sidecar in `_open_sidecar`. `O_CREAT`'s mode argument is masked by
   umask; `fchmod` after the fact is not. Stated here as a whole-module property
   because the first version protected only the config, and the comment saying so
   sat one function away from the sidecar that still had the bug — a reader who
   found that comment reasonably concluded the module was umask-safe throughout.
   It was not, for four hours, and only a root-umask measurement caught it
   (orchestrator, T-2329). If you add a third file, fchmod it too.

Not a general-purpose config library — it is the smallest thing that closes the
lost-update hole, so that two seats (or two agents) editing one config converge
instead of one silently winning.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

__all__ = ["edit_json", "LockTimeout"]

DEFAULT_TIMEOUT_SECS = 10.0
_POLL_SECS = 0.02


class LockTimeout(RuntimeError):
    """Could not acquire the config lock within the timeout."""


def _open_sidecar(lock_path: Path, lock_mode: int) -> int:
    """Open the lock sidecar, applying `lock_mode` ONLY if we are the creator.

    `O_CREAT`'s mode argument is masked by umask, so `O_CREAT, 0o660` yields 0640
    under the common umask 022 — group-WRITE is stripped. Measured by orchestrator as root
    (0640 in the wild) and reproduced here.

    ⚠ CORRECTION TO THE ORIGINAL DIAGNOSIS: it is NOT true that "flock needs write
    access." `flock()` places a lock regardless of the fd's access mode — verified,
    `LOCK_EX` succeeds on an `O_RDONLY` fd. The EACCES on a root-created 0640
    sidecar came from `os.open(..., O_RDWR)`; flock was never reached. So the
    stripped group-write was never the thing locking peers out — opening for write
    we do not need was. Both are fixed: `fchmod` restores the intended mode on
    creation, and the sidecar is opened `O_RDONLY` (see below), which is what makes
    a 0640 sidecar usable by a group peer with no chown at all.

    This is the same O_CREAT-vs-umask trap the config write below explicitly
    avoids; the first version of this module defended the config against it and
    left it in the sidecar. `fchmod` after creation is the symmetric fix — umask
    does not get to narrow a mode we are stating deliberately.

    `O_EXCL` distinguishes create from open so we NEVER re-mode a sidecar someone
    else created: their mode governs, and silently widening another account's lock
    file would be its own defect. A pre-existing too-narrow sidecar is the caller's
    chown problem (see `edit_json`'s docstring), not something to paper over here.
    """
    try:
        fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT | os.O_EXCL, lock_mode)
    except FileExistsError:
        # O_RDONLY IS DELIBERATE, and it is what actually reopens the cross-account
        # door. flock() places a lock regardless of the fd's access mode — verified:
        # LOCK_EX succeeds on an O_RDONLY fd — and we never write to the sidecar, so
        # write access buys nothing. Opening O_RDWR is what raised EACCES on a
        # root-created 0640 sidecar; the lock call itself was never reached. With
        # O_RDONLY a group peer needs only group-READ, so the 0640 that umask leaves
        # behind is usable rather than a lockout.
        return os.open(lock_path, os.O_RDONLY)  # not ours to re-mode
    try:
        os.fchmod(fd, lock_mode)
    except OSError:
        pass  # best-effort: a filesystem that refuses chmod is not a lock failure
    return fd


def _acquire(lock_path: Path, timeout: float, lock_mode: int) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_sidecar(lock_path, lock_mode)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise LockTimeout(
                    f"{lock_path} held by another writer for >{timeout}s. "
                    "Not reaping: the other writer is a peer, not a corpse."
                ) from exc
            time.sleep(_POLL_SECS)


@contextmanager
def edit_json(path, *, timeout: float = DEFAULT_TIMEOUT_SECS, indent: int = 2,
              missing_ok: bool = True, create_mode: int = 0o600,
              lock_mode: int = 0o660):
    """Yield the parsed JSON at `path` for in-place mutation; write it back atomically.

    The read, the caller's mutation, and the write all happen under one exclusive
    lock on `<path>.lock`, so concurrent editors serialize instead of clobbering.
    An exception from the caller's block aborts the write entirely — the file is
    left exactly as it was, not partially updated.

    `missing_ok=True` (default) treats an absent file as `{}` so first-write is not
    a special case at every call site.

    PERMISSIONS. An EXISTING file keeps its own mode; `create_mode` applies only
    when creating one. This is not incidental: `os.replace` swaps the inode, so
    the tmp file's mode BECOMES the config's mode. The first version of this
    module wrote the tmp via `open(tmp, "w")` and therefore silently republished
    a 0600 config as 0664 — measured, a permission WIDENING on a file that can
    carry secrets-adjacent settings.

    `lock_mode` is 0o660 because the sidecar is contended across ACCOUNTS as well
    as processes and the fleet shares the `veneration` group.
    ⚠ CROSS-ACCOUNT: this mode applies at CREATION only — `O_CREAT` will not
    loosen an existing file. Whoever creates the sidecar first fixes its owner and
    group for good, so a root-run fleet script that creates it 0600 root:root
    locks the owning seat out of its OWN settings permanently, and the seat cannot
    repair it without privilege. A privileged writer must create the sidecar
    group-writable and chown it to the target account. This module cannot do that
    itself (it would need privilege it should not have): it is the one integration
    point the caller must handle.
    """
    path = Path(path)
    fd = _acquire(Path(str(path) + ".lock"), timeout, lock_mode)
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not missing_ok:
                raise
            data = {}

        yield data  # caller mutates `data` in place; raising aborts the write

        # os.replace swaps the inode, so the tmp file's mode becomes the
        # config's mode. Carry the existing one across rather than minting a
        # fresh 0666-and-umask default.
        try:
            out_mode = os.stat(path).st_mode & 0o7777
        except FileNotFoundError:
            out_mode = create_mode

        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), out_mode)   # fchmod, not O_CREAT: umask does
                                               # not get to narrow a mode we are
                                               # explicitly preserving
            json.dump(data, fh, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())          # survive a crash between write and replace
        os.replace(tmp, path)              # atomic for readers: never a half-file
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)               # durable rename, not just durable bytes
        finally:
            os.close(dir_fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
