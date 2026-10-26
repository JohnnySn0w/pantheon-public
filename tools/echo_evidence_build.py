#!/usr/bin/env python3
"""Run an Echo evidence build (review smoke, merge gate) in a per-worktree isolated CARGO_TARGET_DIR (T-4122).

THE FAILURE. Two worktrees building on the shared /var/lib/pantheon/echo-target
silently reuse each other's workspace-crate artifacts: cargo's artifact hash for
a path source is relocatable (no absolute path in it), and freshness is judged
by mtime, so a peer's newer artifact reads as "fresh" for YOUR sources and a
gate reports another branch's result (bugs/shared-cargo-target-cross-worktree-
artifact-bleed.md).

THE FIX, by construction. Every evidence build runs with
    CARGO_TARGET_DIR = <root>/<basename>-<sha256(realpath(worktree))[:12]>
so distinct worktrees (even two on the same branch) never share a target dir,
while re-gating the same worktree after a rebound reuses its warm dir.

    python3 tools/echo_evidence_build.py [--worktree PATH] [--dry-run] -- <cmd...>
    python3 tools/echo_evidence_build.py --list
    python3 tools/echo_evidence_build.py --gc [--older-than-days N] [--dry-run]

Env: ECHO_EVIDENCE_TARGET_ROOT (default /var/lib/pantheon/echo-evidence-targets),
ECHO_EVIDENCE_DISK_FLOOR_GB (default 12), ECHO_EVIDENCE_FORCE=1 (skip the floor),
ECHO_EVIDENCE_SCCACHE=1 (OPT-IN sccache; default off, RUSTC_WRAPPER cleared).
The speed win is the persistent per-worktree target (warm re-gate 73s clippy +
121s test vs 865s + 1138s cold), not sccache (see sccache_functional()).

Every real (non-dry-run) invocation first reaps THIS seat's ORPHANED evidence
dirs (recorded worktree stat() == ENOENT; never the current one, never by age,
never one with a held cargo build lock) best-effort, so worktree removal needs
no timer; age-based GC and other seats' dirs stay exclusive to explicit --gc.

No-meta backstop (T-4795): a dir with no .evidence-meta.json (built with a raw
CARGO_TARGET_DIR, bypassing this tool) is still never deleted UNLESS its name is
<basename>-<digest> for a ticket worktree (echo-T-NNNN... in /tmp, T-NNNN... in
~/echo-prismatic/.claude/worktrees) whose path re-hashes to exactly that digest,
that worktree's stat() is ENOENT, the dir is owned by this uid, its whole tree
is readable and nothing under it was written in the last 6h (fixed; the --gc
age cutoff does not extend it), no cargo build lock is held, and no live process
references it in /proc (cwd, argv, environ). Both GC paths apply it; each such
reap is logged as "gc: reap <name> (no meta; worktree gone)", and every keep
with its reason under --gc. --gc --dry-run reports what would be removed and
deletes nothing.

Source/target consistency (refused, exit 64): the cwd must be inside the
worktree (cargo builds the cwd's sources), any --manifest-path must resolve
inside it, and no argv token may contain --target-dir (substring match, so
`bash -c '... --target-dir ...'` is caught). NOT detectable: a cargo [alias]
or config that embeds --target-dir / build.target-dir.

The child is exec'd (not spawned), so its exit status IS this tool's exit
status and signals reach cargo directly. Tool-side refusals: 64 usage / not a
worktree / source mismatch, 75 disk floor, 126 not executable, 127 not found.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_ROOT = "/var/lib/pantheon/echo-evidence-targets"
# The shared dev-profile target. GC must never touch it, nor anything inside it.
SHARED_TARGET = Path("/var/lib/pantheon/echo-target")
META_NAME = ".evidence-meta.json"
META_TOOL = "echo_evidence_build"
DEFAULT_FLOOR_GB = 12.0
DEFAULT_GC_DAYS = 14
# sccache --show-stats connects to (and if needed starts) the local server; a
# healthy cold start is ~1-2s. 5s bounds a wedged daemon without false-failing.
SCCACHE_PROBE_TIMEOUT_S = 5.0
# T-4795 no-meta backstop: a dir built with a raw CARGO_TARGET_DIR has no meta,
# so it is reaped only if its NAME proves (by re-hashing) which worktree it was
# keyed on, that worktree is gone, and nothing has written to it for this long.
# 6h: far above any single evidence gate (cold workspace gate 30-60+ min, the
# push hook's budget is 2h), so a dir a raw build is still filling (or one whose
# worktree is being re-created at the same path) is never reaped mid-use, while
# a true orphan is still reclaimed the same day.
NOMETA_GRACE_S = 6 * 3600
# <basename>-<sha256[:12]> (target_dir_for) where basename encodes a ticket
# worktree: /tmp/echo-T-NNNN[...] or <home>/echo-prismatic/.claude/worktrees/T-NNNN[...].
_NOMETA_NAME_RE = re.compile(r"^(?P<base>(?:echo-)?T-[0-9]+[A-Za-z0-9._-]*)-(?P<digest>[0-9a-f]{12})$")

EX_USAGE = 64
EX_DISK = 75
EX_NOEXEC = 126
EX_NOTFOUND = 127

_THIS = Path(__file__).resolve()


def _say(msg: str) -> None:
    print(f"echo-evidence-build: {msg}", file=sys.stderr)


# ------------------------------------------------------------------ resolution

def target_root() -> Path:
    return Path(os.environ.get("ECHO_EVIDENCE_TARGET_ROOT") or DEFAULT_ROOT)


def resolve_worktree(path: str | None) -> Path | None:
    """Realpath of the git worktree top-level containing `path` (cwd if None)."""
    cwd = path if path is not None else os.getcwd()
    if not os.path.isdir(cwd):
        return None
    try:
        p = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = p.stdout.strip()
    if p.returncode != 0 or not top:
        return None
    return Path(os.path.realpath(top))


def target_dir_for(worktree: Path, root: Path) -> Path:
    real = os.path.realpath(str(worktree))
    digest = hashlib.sha256(real.encode()).hexdigest()[:12]
    return root / f"{os.path.basename(real)}-{digest}"


def free_gb(path: Path) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free / (1024 ** 3)


# ------------------------------------------------------------------ sccache

def sccache_functional() -> bool:
    """FUNCTIONAL probe: `sccache --show-stats` exits 0 in time. `command -v`
    alone is not enough (installed-but-no-server / wedged-daemon both pass it).

    Why sccache is only a speed layer UNDER isolation and never the bleed fix:
    on a SHARED target, cargo decides a unit is fresh from its fingerprint +
    mtimes BEFORE it would invoke rustc (and so the wrapper); a peer's
    fresh-looking artifact is reused without sccache ever being asked. sccache
    keys on content, but it only sees the compiles cargo chooses to run.

    Why it is OPT-IN (ECHO_EVIDENCE_SCCACHE=1), measured by artisan on T-4122:
    across worktrees/targets it scored 0 Rust hits (925 misses) and ~20% C/C++
    hits. Its Rust keys include the absolute out-dir and source paths, so a
    per-worktree isolated target can never hit. Net overhead: clippy 1587s vs 865s
    without, test 1475s vs 1138s (under concurrent load, but the direction is
    clear). The probe stays for the opt-in path only.
    """
    exe = shutil.which("sccache")
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "--show-stats"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=SCCACHE_PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


# ------------------------------------------------------------------ meta

def make_meta(worktree: str, last_used: float, created: float | None = None) -> dict:
    iso = _dt.datetime.fromtimestamp(last_used, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"tool": META_TOOL, "schema": 1, "worktree": worktree,
            "created_epoch": created if created is not None else last_used,
            "last_used_epoch": last_used, "last_used": iso}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def read_meta(d: Path) -> dict | None:
    """Our meta, type-validated; None (= never delete) for anything else."""
    try:
        m = json.loads((d / META_NAME).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("tool") != META_TOOL:
        return None
    wt = m.get("worktree")
    if not isinstance(wt, str) or not wt or not os.path.isabs(wt):
        return None
    if "last_used_epoch" in m and not _is_num(m["last_used_epoch"]):
        return None
    if "created_epoch" in m and not _is_num(m["created_epoch"]):
        return None
    return m


def write_meta(d: Path, worktree: str) -> None:
    old = read_meta(d)
    now = time.time()
    meta = make_meta(worktree, now, old.get("created_epoch") if old else None)
    tmp = d / f"{META_NAME}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(meta, indent=1) + "\n")
    os.replace(tmp, d / META_NAME)  # rename: works over a peer seat's file (group-writable dir)


def _ensure_dir(d: Path) -> None:
    """Create with group-writable setgid perms (README-echo-target.md: cross-seat
    reuse needs g+w; setgid keeps group pantheon-shared)."""
    missing = []
    p = d
    while not p.exists():
        missing.append(p)
        p = p.parent
    old = os.umask(0o002)
    try:
        d.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(old)
    for m in reversed(missing):
        try:
            os.chmod(m, 0o2775)
        except OSError:
            pass


# ------------------------------------------------------------------ run

def _exec(cmd: list[str], env: dict[str, str]) -> int:
    os.umask(0o002)  # inherited across exec: artifacts land 0664/2775
    try:
        os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError:
        _say(f"command not found: {cmd[0]}")
        return EX_NOTFOUND
    except PermissionError as e:  # EACCES: found but not executable (shell convention)
        _say(f"exec failed: {e}")
        return EX_NOEXEC
    except OSError as e:
        _say(f"exec failed: {e}")
        return EX_NOTFOUND
    return 0  # unreachable


def _floor_gb() -> float:
    try:
        return float(os.environ.get("ECHO_EVIDENCE_DISK_FLOOR_GB") or DEFAULT_FLOOR_GB)
    except ValueError:
        return DEFAULT_FLOOR_GB


def _manifest_paths(cmd: list[str]) -> list[str]:
    out = []
    for i, a in enumerate(cmd):
        if a == "--manifest-path":
            out.append(cmd[i + 1] if i + 1 < len(cmd) else "")
        elif a.startswith("--manifest-path="):
            out.append(a.split("=", 1)[1])
    return out


def _check_sources_match(wt: Path, cmd: list[str]) -> str | None:
    """Cargo builds the CWD's (or --manifest-path's) sources, but the target dir
    is keyed on the worktree: a mismatch is the same bleed class. Returns a
    refusal message, or None if consistent."""
    cwd_wt = resolve_worktree(None)
    if cwd_wt != wt:
        return (f"refusing: cwd {os.getcwd()} is not inside the worktree {wt} "
                f"(cwd resolves to {cwd_wt or 'no git worktree'}); cargo would build the "
                f"cwd's sources into {wt}'s target dir. cd into the worktree first.")
    for mp in _manifest_paths(cmd):
        real = Path(os.path.realpath(mp)) if mp else None
        if real is None or not _is_within(real, wt):
            return f"refusing: --manifest-path {mp!r} resolves outside the worktree {wt}"
        parent = real.parent
        if parent.is_dir() and resolve_worktree(str(parent)) != wt:
            return f"refusing: --manifest-path {mp!r} is in a different (nested) worktree than {wt}"
    return None


def run(worktree_arg: str | None, cmd: list[str], dry_run: bool) -> int:
    if not cmd:
        _say("no command given (usage: ... -- <command...>)")
        return EX_USAGE
    # Substring match also catches `bash -c '... --target-dir ...'`. NOT detectable:
    # a cargo [alias] (or config) that embeds --target-dir / build.target-dir.
    if any("--target-dir" in a for a in cmd):
        _say("refusing: the command passes --target-dir, which would defeat the isolation")
        return EX_USAGE
    try:
        os.getcwd()
    except OSError as e:  # cwd deleted under us (e.g. a removed worktree)
        _say(f"refusing: cwd is unavailable ({e.strerror or e}); cd into the worktree first")
        return EX_USAGE
    wt = resolve_worktree(worktree_arg)
    if wt is None:
        _say(f"not a git worktree: {worktree_arg or os.getcwd()}")
        return EX_USAGE
    msg = _check_sources_match(wt, cmd)
    if msg:
        _say(msg)
        return EX_USAGE
    root = target_root()
    tdir = target_dir_for(wt, root)

    # Orphan-only GC before the floor check, so reclaimed space counts toward it.
    if not dry_run:
        reap_orphans_best_effort(root, current=tdir)

    floor = _floor_gb()
    avail = free_gb(root)
    if avail < floor:
        if os.environ.get("ECHO_EVIDENCE_FORCE") == "1":
            _say(f"WARNING: {avail:.1f}G free < floor {floor:g}G; continuing (ECHO_EVIDENCE_FORCE=1)")
        else:
            _say(f"refusing: only {avail:.1f}G free on {root}'s filesystem (< floor {floor:g}G). "
                 f"Free space with: python3 {_THIS} --gc  (inspect: --list); "
                 f"override: ECHO_EVIDENCE_FORCE=1 or tune ECHO_EVIDENCE_DISK_FLOOR_GB")
            return EX_DISK

    env = dict(os.environ)
    inherited = env.get("CARGO_TARGET_DIR")
    if inherited and inherited != str(tdir):
        _say(f"OVERRIDING inherited CARGO_TARGET_DIR={inherited} -> {tdir} (evidence builds never use a shared target)")
    # CARGO_BUILD_TARGET_DIR is lower-precedence than CARGO_TARGET_DIR; drop it anyway so env is unambiguous.
    env.pop("CARGO_BUILD_TARGET_DIR", None)
    env["CARGO_TARGET_DIR"] = str(tdir)
    env["CARGO_INCREMENTAL"] = "0"
    env.pop("RUSTC_WRAPPER", None)
    env.pop("CARGO_BUILD_RUSTC_WRAPPER", None)

    # OPT-IN only (exactly "1"); see sccache_functional() for the measured reason.
    if os.environ.get("ECHO_EVIDENCE_SCCACHE") != "1":
        sc_note = "sccache: off (opt-in via ECHO_EVIDENCE_SCCACHE=1); RUSTC_WRAPPER unset"
    elif dry_run:
        sc_note = "sccache: not probed (dry-run); RUSTC_WRAPPER unset"
    elif sccache_functional():
        env["RUSTC_WRAPPER"] = "sccache"
        sc_note = "sccache: functional probe OK; RUSTC_WRAPPER=sccache (speed layer under isolation)"
    else:
        sc_note = "sccache: probe failed/absent; RUSTC_WRAPPER unset"

    if dry_run:
        print(f"worktree={wt}")
        for k in ("CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "RUSTC_WRAPPER"):
            print(f"{k}={env.get(k, '<unset>')}")
        print("umask=002")
        print(sc_note)
        print("command: " + " ".join(cmd))
        return 0

    _ensure_dir(tdir)
    write_meta(tdir, str(wt))
    _say(f"CARGO_TARGET_DIR={tdir} (isolated per worktree {wt}); CARGO_INCREMENTAL=0; umask 002")
    _say(sc_note)
    sys.stdout.flush()
    sys.stderr.flush()
    return _exec(cmd, env)


# ------------------------------------------------------------------ gc / list

def _is_within(p: Path, parent: Path) -> bool:
    try:
        p.relative_to(parent)
        return True
    except ValueError:
        return False


def _root_is_safe(root: Path) -> bool:
    rr = Path(os.path.realpath(root))
    shared = Path(os.path.realpath(SHARED_TARGET))
    return not (rr == shared or _is_within(rr, shared))


def _candidates(root: Path):
    """Direct, non-symlink child dirs of root, never the shared target. Callers
    must still require valid own-meta (read_meta) before deleting anything."""
    rr = Path(os.path.realpath(root))
    shared = Path(os.path.realpath(SHARED_TARGET))
    try:
        entries = sorted(os.scandir(rr), key=lambda e: e.name)
    except FileNotFoundError:
        return
    for e in entries:
        if not e.is_dir(follow_symlinks=False):
            continue
        p = Path(os.path.realpath(e.path))
        if p.parent != rr or p == shared or _is_within(shared, p) or _is_within(p, shared):
            continue
        yield p


def _my_uid() -> int:
    return os.getuid()


def _worktree_gone(wt: str) -> bool:
    """Orphan iff stat() says ENOENT. PermissionError (another seat's 0750 home)
    or any other OSError means UNKNOWN -> keep. os.path.exists() is WRONG here:
    it returns False on EACCES and would reap a peer seat's live dir."""
    try:
        os.stat(wt)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _nometa_worktree_parents() -> list[Path]:
    """Where evidence worktrees live (T-4795): /tmp/echo-T-NNNN* and this seat's
    echo-prismatic agent worktrees. Only these are tried when mapping a no-meta
    dir's name back to its worktree."""
    return [Path("/tmp"), Path.home() / "echo-prismatic" / ".claude" / "worktrees"]


def _nometa_worktree(name: str) -> str | None:
    """Map a no-meta dir name back to the ONE worktree path target_dir_for()
    would have keyed it on, or None (= unknown -> keep). The candidate must
    reproduce the name's digest exactly (sha256 of its realpath), so a name that
    merely looks right, or matches more than one candidate, is never mapped."""
    m = _NOMETA_NAME_RE.match(name)
    if not m:
        return None
    hits = []
    for parent in _nometa_worktree_parents():
        cand = os.path.join(os.path.realpath(str(parent)), m["base"])
        if hashlib.sha256(cand.encode()).hexdigest()[:12] == m["digest"]:
            hits.append(cand)
    return hits[0] if len(hits) == 1 else None


def _newest_mtime(d: Path) -> float | None:
    """Newest lstat mtime of d and everything under it (no symlink following).
    None if any part of the tree can't be read (ambiguous -> caller keeps)."""
    unreadable = []
    newest = os.lstat(d).st_mtime
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False,
                                                onerror=unreadable.append):
        for n in filenames + dirnames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, n)).st_mtime)
            except FileNotFoundError:
                pass
            except OSError as e:
                unreadable.append(e)
    return None if unreadable else newest


PROC = Path("/proc")
_PATH_CONT = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def _proc_refs() -> list[str]:
    """Strings live processes could reference a target dir by (T-4795): every
    readable cwd link, argv token, and environ value under PROC. Unreadable
    entries (other uids' cwd/environ, exited pids) are skipped: argv is
    world-readable so rustc's --out-dir/-L still shows, and the cargo build
    lock check covers the rest."""
    refs: list[str] = []
    try:
        pids = [e.name for e in os.scandir(PROC) if e.name.isdigit()]
    except OSError:
        return refs
    for pid in pids:
        base = PROC / pid
        try:
            refs.append(os.readlink(base / "cwd"))
        except OSError:
            pass
        for fname in ("cmdline", "environ"):
            try:
                raw = (base / fname).read_bytes()
            except OSError:
                continue
            for tok in raw.split(b"\0"):
                if not tok:
                    continue
                s = tok.decode("utf-8", "replace")
                refs.append(s.split("=", 1)[1] if fname == "environ" and "=" in s else s)
    return refs


def _referenced(paths: list[str], refs: list[str]) -> bool:
    """True if any ref contains one of paths as a whole path component prefix
    (followed by end, '/', or a non-name char), so echo-T-1-<d> never matches
    echo-T-1-<d>x."""
    for ref in refs:
        for path in paths:
            i = ref.find(path)
            while i != -1:
                j = i + len(path)
                if j == len(ref) or ref[j] not in _PATH_CONT:
                    return True
                i = ref.find(path, i + 1)
    return False


def _build_in_flight(p: Path) -> bool:
    """True if any cargo build lock under p is held (cargo flock()s
    <target>/<profile>/.cargo-lock for the build). Can't open -> held."""
    import fcntl
    # depth 1: <profile>/.cargo-lock; depth 2: <triple>/<profile>/.cargo-lock (--target builds)
    locks = [p / ".cargo-lock", *p.glob("*/.cargo-lock"), *p.glob("*/*/.cargo-lock")]
    for lk in locks:
        if not lk.exists():
            continue
        try:
            fd = os.open(lk, os.O_RDONLY)
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return False


def _du_bytes(d: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(d, followlinks=False):
        for n in filenames + dirnames:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_blocks * 512
            except OSError:
                pass
    return total


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}"


def cmd_list() -> int:
    root = target_root()
    rows = list(_candidates(root))
    if not rows:
        print(f"(no evidence target dirs under {root})")
        return 0
    for p in rows:
        meta = read_meta(p)
        wt = meta["worktree"] if meta else "(no/invalid meta)"
        used = meta.get("last_used", "?") if meta else "?"
        gone = "  [ORPHAN]" if meta and _worktree_gone(meta["worktree"]) else ""
        print(f"{_human(_du_bytes(p)):>8}  {used}  {p.name}  {wt}{gone}")
    return 0


def _rmtree_racy(p: Path) -> None:
    """rmtree where a concurrent remover (another seat's GC) winning is success:
    FileNotFoundError anywhere in the walk means already-gone, not failure."""
    def onexc(_fn, _path, exc):
        if not isinstance(exc, FileNotFoundError):
            raise exc
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=onexc)
        else:  # CI's scheduled matrix still runs 3.11 (onerror passes exc_info)
            shutil.rmtree(p, onerror=lambda fn, path, ei: onexc(fn, path, ei[1]))
    except FileNotFoundError:
        pass


def _reap(root: Path, cutoff: float | None, keep: Path | None, verbose: bool,
          own_only: bool = False, now: float | None = None, dry_run: bool = False):
    """The ONE safety predicate for both GC paths. A dir is removed only if ALL:
    direct non-symlink child of root, not the shared target (_candidates);
    not `keep`; no in-flight cargo build lock; and EITHER
      (meta) valid own meta (read_meta); if own_only, meta file owned by this
      uid; and worktree stat() == ENOENT (orphan) or, with a cutoff, a numeric
      last_used_epoch older than it;
    OR (no meta, T-4795 backstop) the name maps to exactly one worktree via
      _nometa_worktree (digest re-verified); the dir itself is owned by this
      uid (on BOTH paths); that worktree's stat() == ENOENT; the whole tree is
      readable and its newest mtime is older than now - NOMETA_GRACE_S (a
      FIXED window on both paths: --older-than-days never delays it, like
      meta'd worktree-gone dirs); and no live process references it under
      PROC (cwd, argv, environ — _proc_refs).
    Anything else is never deleted. Each dir is handled in its own try, so one
    bad dir can't abort the sweep. dry_run: same decisions, nothing removed.
    Returns (removed [(name, why, bytes)], errors [str])."""
    removed, errors = [], []
    refs: list[str] | None = None  # lazily scanned once per sweep
    grace_cut = (time.time() if now is None else now) - NOMETA_GRACE_S
    keep_real = Path(os.path.realpath(keep)) if keep is not None else None
    for p in _candidates(root):
        try:
            if keep_real is not None and p == keep_real:
                continue
            meta = read_meta(p)
            if meta is None:
                wt = _nometa_worktree(p.name)
                if wt is None:
                    if verbose:
                        kind = "invalid" if os.path.lexists(p / META_NAME) else "no"
                        _say(f"gc: skip {p.name} ({kind} {META_NAME}; never deleted)")
                    continue
                if os.lstat(p).st_uid != _my_uid():
                    if verbose:
                        _say(f"gc: skip {p.name} (no meta; not owned by this uid)")
                    continue
                if not _worktree_gone(wt):
                    if verbose:
                        _say(f"gc: skip {p.name} (no meta; worktree {wt} present or unknown)")
                    continue
                newest = _newest_mtime(p)
                if newest is None:
                    if verbose:
                        _say(f"gc: skip {p.name} (no meta; tree not fully readable)")
                    continue
                if newest >= grace_cut:
                    if verbose:
                        _say(f"gc: skip {p.name} (no meta; worktree gone but written within the last "
                             f"{NOMETA_GRACE_S // 3600}h)")
                    continue
                if refs is None:
                    refs = _proc_refs()
                if _referenced([str(p), str(root / p.name)], refs):
                    if verbose:
                        _say(f"gc: skip {p.name} (no meta; referenced by a live process)")
                    continue
                why = f"no meta; worktree gone: {wt}"
            else:
                if own_only and os.lstat(p / META_NAME).st_uid != _my_uid():
                    continue
                wt = meta["worktree"]
                if _worktree_gone(wt):
                    why = f"orphan (worktree {wt} gone)"
                elif cutoff is not None and meta.get("last_used_epoch", cutoff) < cutoff:
                    why = f"stale (last used {meta.get('last_used', '?')})"
                else:
                    continue
            if _build_in_flight(p):
                if verbose:
                    _say(f"gc: skip {p.name} (cargo build lock held: in-flight build)")
                continue
            if meta is None and verbose:
                _say(f"gc: {'would reap' if dry_run else 'reap'} {p.name} (no meta; worktree gone)")
            size = _du_bytes(p)
            if not dry_run:
                _rmtree_racy(p)
            removed.append((p.name, why, size))
        except Exception as e:  # noqa: BLE001 — per-dir isolation by contract
            errors.append(f"{p.name}: {type(e).__name__}: {e}")
    return removed, errors


def reap_orphans_best_effort(root: Path, current: Path) -> None:
    """Opportunistic orphan-only GC at the start of a real run, over THIS seat's
    own dirs only (other seats' dirs are left to explicit --gc). Best-effort:
    never raises, never blocks or alters the build; one line or silence."""
    try:
        if not _root_is_safe(root):
            return
        removed, errors = _reap(root, cutoff=None, keep=current, verbose=False,
                                own_only=True)
        if removed:
            total = sum(s for _, _, s in removed)
            _say(f"reclaimed {_human(total)} from {len(removed)} orphaned evidence dir(s): "
                 + ", ".join(n + (" (no meta; worktree gone)" if w.startswith("no meta") else "")
                             for n, w, _ in removed))
        if errors:
            _say("orphan-gc (non-fatal) error: " + "; ".join(errors))
    except Exception as e:  # noqa: BLE001 — best-effort by contract
        _say(f"orphan-gc (non-fatal) error: {type(e).__name__}: {e}")


def cmd_gc(older_than_days: float, dry_run: bool = False) -> int:
    root = target_root()
    if not _root_is_safe(root):
        _say(f"refusing --gc: root {root} is (inside) the shared target {SHARED_TARGET}")
        return EX_USAGE
    removed, errors = _reap(root, cutoff=time.time() - older_than_days * 86400,
                            keep=None, verbose=True, dry_run=dry_run)
    for name, why, size in removed:
        if dry_run:
            _say(f"gc: would remove {name} [{why}] would free {_human(size)}")
        else:
            _say(f"gc: removed {name} [{why}] freed {_human(size)}")
    if dry_run:
        _say(f"gc: dry-run: {len(removed)} dir(s), {_human(sum(s for _, _, s in removed))} "
             "would be freed; nothing deleted")
    for err in errors:
        _say(f"gc: FAILED on {err}")
    return 1 if errors else 0


# ------------------------------------------------------------------ cli

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="echo_evidence_build.py",
                                 description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--gc", action="store_true", help="remove orphaned / stale evidence target dirs")
    mode.add_argument("--list", action="store_true", help="list evidence target dirs")
    ap.add_argument("--older-than-days", type=float, default=DEFAULT_GC_DAYS)
    ap.add_argument("--worktree")
    ap.add_argument("--dry-run", action="store_true")
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EX_USAGE if e.code else 0
    if a.gc:
        return cmd_gc(a.older_than_days, a.dry_run)
    if a.list:
        return cmd_list()
    return run(a.worktree, cmd, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
