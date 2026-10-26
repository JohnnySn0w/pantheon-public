#!/usr/bin/env python3
"""echo_target_adopt.py — re-own a PEER seat's build-script OUT files in the shared
cargo target, so this seat's build scripts can overwrite them (T-4071).

THE FAILURE. echo's CARGO_TARGET_DIR is shared across seats (T-3604/T-3777:
/var/lib/pantheon/echo-target, setgid pantheon-shared, umask 002, no sticky bit).
When a build script RE-RUNS and std::fs::copy()s a generated header/binding/
resource into an OUT_DIR file that ANOTHER seat created, the copy fails:
fs::copy fchmod()s the destination to the source's mode, and fchmod needs
OWNERSHIP, so group-write can never be enough ("Operation not permitted", os
error 1). A peer's 0644 file cannot even be opened for write (os error 13).
Hit 2026-09-28 by artisan on aws-lc-sys, libsqlite3-sys, openssl-sys, ring,
x11-dl and echo-demo-app (see bugs/shared-cargo-target-fs-copy-eperm-foreign-
owned-out-dir.md).

THE FIX (no root, no new account). Every OUT directory is group-writable and
the tree has no sticky bit, so any seat may RENAME within it. For each regular
file this seat does not own: copy it beside itself (data + mode + timestamps)
and rename the copy over the original. The file is now this seat's own, with
the same bytes and mtime, so cargo freshness is untouched (no build-script
re-run, no recompile — unlike `rm -rf build/<crate>-<hash>`). Verified live,
suborch2 over a claude-owned 0644 file in a claude-owned OUT dir: fchmod EPERM
before, OK after.

Ownership then sits with THIS seat, so the next seat to build adopts in turn.
Run it at the start of any echo build entrypoint (the pre-push gate, or before
an ad-hoc cargo build that hit os error 1).

CARGO LOCK (T-4955). Adopt holds the same flock(2) cargo takes on
<profile>/.cargo-lock while it works on that profile, waiting up to --lock-timeout.
Without it, a peer's adopt could re-own OUT files in the MIDDLE of this seat's
cargo build, and the build script that later re-copies them failed with os error
13 or 1 (orchestrator's pre-push, 2026-10-13, aws-lc-sys, while suborch2 built on the
same target). Cargo holds that lock for a whole invocation, so with it no adopt
can land inside a build. A gate that runs several cargo commands must still adopt
before EACH of them: a peer can build in the gaps between them. If the lock is not
acquired in time, that profile is skipped and reported, never failed (a dry run
does not lock). A profile with no .cargo-lock yet is adopted without one.

SAFETY. Files modified in the last --min-age seconds are skipped (a peer's build
may be writing them). A file that changes while it is being copied is left
alone. Symlinks and non-regular files are never touched. Per-file errors are
reported but never fail the run (exit 0): this runs in front of a build and
must not block it. Exit 2 only on a usage error or a missing target.

Default scope is the known copier crates. --all adopts every foreign-owned OUT
file (~1.9 GB on 2026-09-28: a near-full copy, so use it deliberately).
"""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import stat
import sys
import time
from pathlib import Path

DEFAULT_TARGET = "/var/lib/pantheon/echo-target"
# Build scripts observed fs::copy()ing into OUT_DIR on re-run (T-4071).
KNOWN_COPIERS = ("aws-lc-sys", "libsqlite3-sys", "openssl-sys", "ring", "x11-dl", "echo-demo-app")
MIN_AGE_SECONDS = 120
# A peer build can hold the profile lock for ~20 min; this seat's cargo would wait
# that long anyway, so waiting here costs nothing extra.
LOCK_TIMEOUT_SECONDS = 1800.0


def out_dirs(target: Path, packages: set[str] | None) -> list[Path]:
    """Every build-script OUT dir: <target>/<profile>/build/<pkg>-<hash>/out, plus
    the cross-compile layout <target>/<triple>/<profile>/build/<pkg>-<hash>/out.
    `packages` None = all."""
    found: list[Path] = []
    for pattern in ("*/build/*/out", "*/*/build/*/out"):
        for d in target.glob(pattern):
            if not d.is_dir() or d.is_symlink():
                continue
            pkg = d.parent.name.rsplit("-", 1)[0]
            if packages is None or pkg in packages:
                found.append(d)
    return sorted(set(found))


class Tally:
    def __init__(self) -> None:
        self.adopted = 0
        self.bytes = 0
        self.young = 0
        self.changed = 0
        self.errors: list[str] = []
        self.locked_out: list[str] = []
        self.waited = 0.0


def acquire_cargo_lock(profile: Path, timeout: float) -> tuple[int | None, bool]:
    """(fd holding flock on <profile>/.cargo-lock, acquired). (None, True) when the
    profile has no lock file yet (no cargo has built there: nothing to wait for);
    (None, False) when another holder kept it past `timeout`."""
    try:
        fd = os.open(profile / ".cargo-lock", os.O_RDWR)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, True
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None, False
            time.sleep(0.2)


def adopt_file(path: Path, st: os.stat_result, tally: Tally, *, dry_run: bool) -> None:
    if dry_run:
        tally.adopted += 1
        tally.bytes += st.st_size
        return
    tmp = path.with_name(f".adopt.{os.getpid()}.{path.name}")
    try:
        shutil.copy2(path, tmp, follow_symlinks=False)   # data + mode + atime/mtime
        now_st = os.lstat(path)
        if (now_st.st_ino, now_st.st_mtime_ns, now_st.st_size) != (st.st_ino, st.st_mtime_ns, st.st_size):
            tmp.unlink(missing_ok=True)       # a peer rewrote it mid-copy: leave theirs
            tally.changed += 1
            return
        os.replace(tmp, path)
        tally.adopted += 1
        tally.bytes += st.st_size
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        tally.errors.append(f"{path}: {exc}")


def adopt(target: Path, packages: set[str] | None, *, min_age: float = MIN_AGE_SECONDS,
          dry_run: bool = False, my_uid: int | None = None, now: float | None = None,
          lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> Tally:
    """Re-own every foreign-owned regular file under the selected OUT dirs, one
    profile at a time under that profile's cargo lock (dry runs take no lock).
    `my_uid` / `now` are test seams (default: this process's uid / wall clock)."""
    my_uid = os.getuid() if my_uid is None else my_uid
    tally = Tally()
    by_profile: dict[Path, list[Path]] = {}
    for out in out_dirs(target, packages):
        by_profile.setdefault(out.parent.parent.parent, []).append(out)  # out/<pkg>-H/build/<profile>
    for profile, outs in by_profile.items():
        fd = None
        if not dry_run:
            started = time.monotonic()
            fd, ok = acquire_cargo_lock(profile, lock_timeout)
            tally.waited += time.monotonic() - started
            if not ok:
                tally.locked_out.append(str(profile))
                continue
        try:
            _adopt_outs(outs, tally, min_age=min_age, dry_run=dry_run, my_uid=my_uid,
                        now=time.time() if now is None else now)
        finally:
            if fd is not None:
                os.close(fd)
    return tally


def _adopt_outs(outs: list[Path], tally: Tally, *, min_age: float, dry_run: bool,
                my_uid: int, now: float) -> None:
    for out in outs:
        for root, _dirs, files in os.walk(out):
            for name in files:
                if name.startswith(".adopt."):
                    continue                  # another adopter's in-flight temp
                path = Path(root) / name
                try:
                    st = os.lstat(path)
                except OSError as exc:
                    tally.errors.append(f"{path}: {exc}")
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_uid == my_uid:
                    continue
                if now - st.st_mtime < min_age:
                    tally.young += 1
                    continue
                adopt_file(path, st, tally, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", type=Path,
                   default=Path(os.environ.get("CARGO_TARGET_DIR") or DEFAULT_TARGET),
                   help="shared cargo target dir (default: $CARGO_TARGET_DIR, else %(default)s)")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--packages", help="comma-separated crate names (default: the known copiers)")
    scope.add_argument("--all", action="store_true", help="every OUT dir (near-full copy — deliberate use)")
    p.add_argument("--min-age", type=float, default=MIN_AGE_SECONDS,
                   help="skip files modified within this many seconds (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="report what would be adopted; change nothing")
    p.add_argument("--lock-timeout", type=float, default=LOCK_TIMEOUT_SECONDS,
                   help="seconds to wait for each profile's cargo lock (default: %(default)s)")
    args = p.parse_args(argv)
    if not args.target.is_dir():
        print(f"[echo_target_adopt] target {args.target} is not a directory", file=sys.stderr)
        return 2
    packages = None if args.all else (
        {s.strip() for s in args.packages.split(",") if s.strip()} if args.packages else set(KNOWN_COPIERS))
    t = adopt(args.target, packages, min_age=args.min_age, dry_run=args.dry_run,
              lock_timeout=args.lock_timeout)
    verb = "would adopt" if args.dry_run else "adopted"
    print(f"[echo_target_adopt] {verb} {t.adopted} file(s), {t.bytes / 1e6:.1f} MB "
          f"(skipped {t.young} young, {t.changed} changed mid-copy; {len(t.errors)} error(s); "
          f"waited {t.waited:.0f}s for cargo lock)")
    for prof in t.locked_out:
        print(f"[echo_target_adopt] SKIPPED {prof}: cargo lock still held after "
              f"{args.lock_timeout:.0f}s (a peer build is running); nothing adopted there", file=sys.stderr)
    for e in t.errors[:20]:
        print(f"[echo_target_adopt] error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
