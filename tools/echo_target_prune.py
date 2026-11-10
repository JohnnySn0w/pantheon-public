#!/usr/bin/env python3
"""echo_target_prune.py -- bounded, UNIT-granular prune of the shared echo cargo target (T-4852).

INTENT. /var/lib/pantheon/echo-target must stay bounded automatically WITHOUT ever
breaking a build. The failure this makes impossible (artisan, 2026-10-10): a FILE-level
mtime prune deleted build-script OUT_DIR files while the unit's fingerprint stayed
fresh, so cargo never re-ran the build script and the build failed.

HOW. Cargo ties a unit together with one 16-hex hash H:
  .fingerprint/<name>-H   build/<name>-H/   deps/*-H[.ext]
A unit is removed from all three places TOGETHER or not at all (cargo-sweep
semantics). incremental/<crate>-<hash> session dirs are pure caches, removed whole.

STALENESS is LAST USE, not mtime: cargo never rewrites an unchanged unit, so
mtime-only would prune every stable dependency weekly. Per file
last_use = max(st_atime, st_mtime) (lstat, never following symlinks); a unit (or
session dir) is stale only when the NEWEST last_use over all its files is older than
--days. This needs a filesystem that refreshes atime (relatime/strictatime); on
noatime the target is REFUSED (never silently falling back to mtime).

SAFETY. Per profile dir the same flock(2) cargo takes on <profile>/.cargo-lock is
held for the whole prune (skip-and-say if not acquired in --lock-timeout).

WHAT IS AND IS NOT GUARANTEED. The guarantee is structural: this tool never deletes
below unit granularity, so it cannot itself create the artisan failure state (a fresh
fingerprint over a build/<pkg>-H/out missing a generated file). The post-check is
narrower than that and does not pretend otherwise: it can only catch a root-output
reference, or a consumed cargo: directive in build/*/output (not warning/error/
rerun-if-*, and not the script's non-directive stdout, which logs intermediates it
deletes itself), to a path under build/ that no longer exists (e.g. a missing OUT_DIR); a missing generated FILE inside an existing OUT_DIR (e.g. for
include!(concat!(env!("OUT_DIR"), ...))) is undetectable because the expected file
set is unknown. On such a hit the unit's build/ + .fingerprint/ are removed to force
a clean re-run.

DELETION ORDER is load-bearing: .fingerprint, then build, then deps. An abort or
error part-way leaves a MISSING fingerprint (cargo rebuilds the unit), never a fresh
fingerprint over missing outputs. A per-unit error is logged, counted, and the run
continues with the next unit/profile; exit codes: 0 ok, 1 repaired, 2 refused,
3 errors occurred (3 wins over 1).

SUPERSEDED (T-5249, opt-in --superseded-hours H): echo's own crates are rebuilt
under a new hash on almost every commit (277 echo-core units in one day, 2026-10-20),
so the shared target filled faster than any age floor >= 1 day can drain it. With H
set, a unit is ALSO stale when it is unused for H hours AND another unit of the same
name was used more recently (an incremental session dir likewise, grouped by crate
name). The newest unit of every name is never pruned this way, and the cost of a
wrong guess is a rebuild, never a broken build: deletion is still whole-unit, under
the cargo lock, in the same order. H is floored at MIN_SUPERSEDED_HOURS.

SIZE CAP (T-6121, opt-in --max-gb N): unit-granular pruning cannot bound a live build's
output (echo-target refilled ~10G/h, 2026-11-04). After the normal prune, if the target is
still over N GB AND the .cargo-lock of EVERY profile can be acquired (all-or-nothing, the
same flock cargo holds for a whole build, so none is running), every profile dir is emptied
while all locks are held. This is the ONLY deletion coarser than a unit, and it is whole-
profile, never partial: EVERY cargo lock file in a profile (.cargo-lock, held shared, and
.cargo-build-lock / .cargo-artifact-lock, held exclusive by cargo 1.97) is kept, since
unlinking one would let a new build lock a fresh inode while an old holder keeps the old
one; so is anything outside the profile dirs. It also skips when any cargo/rustc process
is running on the host (a test binary can run after cargo releases .cargo-lock), and when
the pre-lock size walk itself fails (files churning under a build). On any skip the run
deletes nothing, releases what it got, logs "SKIP cap" and still exits 0 (the disk
monitor, tools/disk_usage_monitor.py, is the alert for the busy case). The noatime refusal still comes first, so the cap does not run
there. The next build simply rebuilds from cold.

Only /var/lib/pantheon/echo-target (or an explicit --target) is accepted;
echo-evidence-targets is refused. Ambiguous linkage keeps the unit. --days is
floored at 1.
"""
import argparse
import fcntl
import os
import re
import shutil
import sys
import tempfile
import time

DEFAULT_TARGET = "/var/lib/pantheon/echo-target"
EVIDENCE_TARGETS = "/var/lib/pantheon/echo-evidence-targets"
MIN_DAYS = 1
MIN_SUPERSEDED_HOURS = 2
DIR_RE = re.compile(r"^(?P<name>.+)-(?P<h>[0-9a-f]{16})$")
FILE_HASH_RE = re.compile(r"-([0-9a-f]{16})(?=\.|$)")
ABS_PATH_RE = re.compile(r"/[^\s=:\"']+")
DIRECTIVE_RE = re.compile(r"^cargo::?(?P<key>[A-Za-z0-9_-]+)[=:]")
IGNORED_DIRECTIVES = {"warning", "error", "rerun-if-changed", "rerun-if-env-changed"}
# Every cargo lock file in a profile dir is kept by the cap (review of #1634): cargo
# 1.97 holds .cargo-lock SHARED and .cargo-build-lock / .cargo-artifact-lock EXCLUSIVE.
CARGO_LOCK_RE = re.compile(r"^\.cargo-.*lock$")


class Refused(Exception):
    """Target is out of scope or unsafe; nothing was touched."""


def _is_under(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def _ls(p):
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def _real_dir(p):
    return os.path.isdir(p) and not os.path.islink(p)


def _lock_only_profile(path):
    """True for the recognizable zero-work layout left by a successful cap clear."""
    try:
        entries = os.listdir(path)
    except OSError:
        return False
    if ".cargo-lock" not in entries:
        return False
    return all(CARGO_LOCK_RE.match(entry)
               and os.path.isfile(os.path.join(path, entry))
               and not os.path.islink(os.path.join(path, entry))
               for entry in entries)


def find_profiles(target):
    """Recognizable profile dirs at target, child, or grandchild depth.

    An active/non-empty profile has a real ``.fingerprint`` directory. A profile
    successfully emptied by the size cap contains only its real Cargo lock files.
    """
    cands = [target]
    for e in _ls(target):
        c = os.path.join(target, e)
        if _real_dir(c):
            cands.append(c)
            cands += [os.path.join(c, x) for x in _ls(c) if _real_dir(os.path.join(c, x))]
    return [c for c in cands
            if _real_dir(os.path.join(c, ".fingerprint")) or _lock_only_profile(c)]


def check_scope(target):
    """Raise Refused unless `target` is a legitimate echo cargo target dir; return realpath."""
    real = os.path.realpath(target)
    if _is_under(real, EVIDENCE_TARGETS) or _is_under(EVIDENCE_TARGETS, real):
        raise Refused("refusing %s: overlaps %s (owned by echo_evidence_build.py)"
                      % (target, EVIDENCE_TARGETS))
    if not os.path.isdir(real):
        raise Refused("refusing %s: not a directory" % target)
    if not find_profiles(real):
        raise Refused("refusing %s: no recognizable cargo profile dirs found" % target)
    return real


def atime_supported(target):
    """True iff reads refresh atime here. Probe: file with atime==mtime==2d ago,
    read it, see whether atime moved. Also reject an explicit noatime mount."""
    try:
        with open("/proc/self/mounts") as f:
            best = ("", "")
            real = os.path.realpath(target)
            for line in f:
                p = line.split()
                if len(p) > 3 and _is_under(real, p[1]) and len(p[1]) >= len(best[0]):
                    best = (p[1], p[3])
            if "noatime" in best[1].split(","):
                return False
    except OSError:
        pass
    try:
        fd, probe = tempfile.mkstemp(prefix=".prune-atime-probe-", dir=target)
    except OSError:
        return False  # cannot prove atime works -> refuse
    try:
        os.close(fd)
        old = time.time() - 2 * 86400
        os.utime(probe, (old, old))
        with open(probe, "rb") as f:
            f.read(1)
        return os.lstat(probe).st_atime > old + 60
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def walk_stats(path):
    """(newest_last_use over non-directory entries, bytes), recursive; hardlinks counted once."""
    newest, total, seen = 0.0, 0, set()

    def visit(p):
        nonlocal newest, total
        st = os.lstat(p)
        if not _real_dir(p):
            # Directories are excluded from last_use: listing them (as this tool does)
            # bumps their atime, which would make every prune poison the next one.
            newest = max(newest, st.st_atime, st.st_mtime)
        key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            total += st.st_blocks * 512
        if _real_dir(p):
            for e in os.listdir(p):
                visit(os.path.join(p, e))

    visit(path)
    return newest, total


def collect_units(profile):
    """{hash: {'name': str, 'paths': [..]}} for hashes that have a .fingerprint dir.

    `paths` is ordered .fingerprint, build, deps -- the deletion order (see module
    docstring: a partial failure must leave a missing fingerprint, never a fresh one
    over missing outputs)."""
    units = {}
    fp = os.path.join(profile, ".fingerprint")
    for e in _ls(fp):
        m = DIR_RE.match(e)
        if m:
            u = units.setdefault(m["h"], {"name": m["name"], "paths": []})
            u["paths"].append(os.path.join(fp, e))
    bd = os.path.join(profile, "build")
    for e in _ls(bd):
        m = DIR_RE.match(e)
        if m and m["h"] in units:
            units[m["h"]]["paths"].append(os.path.join(bd, e))
    dd = os.path.join(profile, "deps")
    for e in _ls(dd):
        hs = set(FILE_HASH_RE.findall(e))
        if len(hs) == 1:  # zero or several candidate hashes = ambiguous linkage: keep
            (h,) = hs
            if h in units:
                units[h]["paths"].append(os.path.join(dd, e))
    return units


def safe_remove(path, profile):
    """Remove path only if it sits strictly inside `profile` (symlinks unlinked, not followed)."""
    parent = os.path.realpath(os.path.dirname(path))
    if not _is_under(parent, profile):
        raise Refused("path escapes profile: %s" % path)
    if _real_dir(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def acquire_lock(profile, timeout):
    """Return an open fd holding flock on <profile>/.cargo-lock, or None."""
    try:
        fd = os.open(os.path.join(profile, ".cargo-lock"), os.O_RDWR)
    except OSError:
        return None
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def _consumed_text(fn, text):
    """The part of a build/<pkg>-H file whose paths cargo/rustc actually consume.
    root-output is the OUT_DIR itself. output is the build script's whole stdout: only
    cargo:/cargo:: directive lines count, and not warning/error (log text) or
    rerun-if-* (a missing path there only makes cargo re-run the script). The rest of
    stdout (make logs, compiler chatter) names intermediates the script itself deletes:
    aws-lc-sys .o files, openssl-sys's build/src (first live run, 2026-10-10)."""
    if fn == "root-output":
        return text
    keep = []
    for line in text.splitlines():
        m = DIRECTIVE_RE.match(line)
        if m and m["key"] not in IGNORED_DIRECTIVES:
            keep.append(line)
    return "\n".join(keep)


def broken_build_units(profile):
    """[(build_dir_name, missing_path)] for build/<pkg>-H whose root-output or consumed
    output directives reference a path under ITS OWN build/<pkg>-H/ that no longer exists.
    A reference into ANOTHER unit's build dir (aws-lc-rs into aws-lc-sys's out/) is not
    this unit's breakage: rebuilding that unit (same hash) recreates the same path. Own-dir
    scope also means one repair can never make another unit look broken (no cascade)."""
    bd = os.path.join(profile, "build")
    bad = []
    for e in _ls(bd):
        own = os.path.join(bd, e)
        found = None
        for fn in ("output", "root-output"):
            p = os.path.join(bd, e, fn)
            if found or not os.path.isfile(p):
                continue
            try:
                with open(p, errors="replace") as fh:
                    text = _consumed_text(fn, fh.read())
            except OSError:
                continue
            for ref in ABS_PATH_RE.findall(text):
                if _is_under(os.path.dirname(ref) or "/", own) and not os.path.lexists(ref):
                    found = ref
                    break
        if found:
            bad.append((e, found))
    return bad


def _stale_reason(name, last, cutoff, sup_cutoff, newest_by_name):
    """Why an entry is stale: 'age', 'superseded', or '' to keep it."""
    if last < cutoff:
        return "age"
    if sup_cutoff is not None and last < sup_cutoff and last < newest_by_name.get(name, last):
        return "superseded"
    return ""


def prune_profile(profile, days, dry_run, lock_timeout, now=None, log=print,
                  superseded_hours=None):
    """Prune one profile. Returns dict(profile, freed, units, incremental, repaired, skipped, errors)."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    sup_cutoff = None if superseded_hours is None else now - superseded_hours * 3600
    res = {"profile": profile, "freed": 0, "units": 0, "incremental": 0,
           "repaired": [], "skipped": False, "errors": 0}
    fd = None
    if not dry_run:
        fd = acquire_lock(profile, lock_timeout)
        if fd is None:
            res["skipped"] = True
            log("SKIP %s: .cargo-lock not acquired within %ss (build running or lock unopenable)"
                % (profile, lock_timeout))
            return res
    pre = "would remove " if dry_run else "remove "
    try:
        # Pass 1 measures every unit so "superseded" can compare against the newest
        # unit of the same name; a unit whose stats fail is kept and counted.
        measured, newest = [], {}
        for h, u in sorted(collect_units(profile).items()):
            try:
                stats = [walk_stats(q) for q in u["paths"]]
            except OSError as e:
                res["errors"] += 1
                log("ERROR %s unit %s-%s: %s (continuing)" % (profile, u["name"], h, e))
                continue
            last = max(s[0] for s in stats)
            newest[u["name"]] = max(newest.get(u["name"], 0.0), last)
            measured.append((h, u, last, sum(s[1] for s in stats)))
        for h, u, last, size in measured:
            try:
                why = _stale_reason(u["name"], last, cutoff, sup_cutoff, newest)
                if not why:
                    continue
                log("%sUNIT %s-%s %d bytes (%d paths)%s" % (
                    pre, u["name"], h, size, len(u["paths"]),
                    " [superseded]" if why == "superseded" else ""))
                if not dry_run:
                    # ORDER IS LOAD-BEARING: paths are .fingerprint, build, deps. If we
                    # stop part-way the fingerprint is already gone (cargo rebuilds), never
                    # a fresh fingerprint over missing outputs.
                    for q in u["paths"]:
                        safe_remove(q, profile)
            except (OSError, Refused) as e:
                res["errors"] += 1
                log("ERROR %s unit %s-%s: %s (continuing)" % (profile, u["name"], h, e))
                continue
            res["units"] += 1
            res["freed"] += size
        inc = os.path.join(profile, "incremental")
        inc_measured, inc_newest = [], {}
        for e in _ls(inc):
            try:
                last, size = walk_stats(os.path.join(inc, e))
            except OSError as ex:
                res["errors"] += 1
                log("ERROR %s incremental %s: %s (continuing)" % (profile, e, ex))
                continue
            name = e.rsplit("-", 1)[0]
            inc_newest[name] = max(inc_newest.get(name, 0.0), last)
            inc_measured.append((e, name, last, size))
        for e, name, last, size in inc_measured:
            q = os.path.join(inc, e)
            try:
                why = _stale_reason(name, last, cutoff, sup_cutoff, inc_newest)
                if not why:
                    continue
                log("%sINCREMENTAL %s %d bytes%s" % (
                    pre, e, size, " [superseded]" if why == "superseded" else ""))
                if not dry_run:
                    safe_remove(q, profile)
            except (OSError, Refused) as ex:
                res["errors"] += 1
                log("ERROR %s incremental %s: %s (continuing)" % (profile, e, ex))
                continue
            res["incremental"] += 1
            res["freed"] += size
        for e, ref in broken_build_units(profile):
            if dry_run:
                log("would REPAIR %s: build/%s references missing %s" % (profile, e, ref))
                res["repaired"].append(e)
                continue
            m = DIR_RE.match(e)
            log("REPAIR %s: build/%s references missing %s; removing .fingerprint/ + build/ "
                "to force a clean re-run" % (profile, e, ref))
            paths = []
            if m:  # fingerprint first, same load-bearing order as above
                fp = os.path.join(profile, ".fingerprint")
                paths += [os.path.join(fp, x) for x in _ls(fp) if x.endswith("-" + m["h"])]
            paths.append(os.path.join(profile, "build", e))
            try:
                for q in paths:
                    if os.path.lexists(q):
                        res["freed"] += walk_stats(q)[1]
                        safe_remove(q, profile)
            except (OSError, Refused) as ex:
                res["errors"] += 1
                log("ERROR %s repair %s: %s (continuing)" % (profile, e, ex))
                continue
            res["repaired"].append(e)
    finally:
        if fd is not None:
            os.close(fd)
    return res


class Results(list):
    """Per-profile result dicts, plus `.cap`: None (cap off) or a dict with
    status in under-cap | cleared | would-clear | skipped-locked | skipped-busy |
    skipped-measure, size/freed bytes."""
    cap = None


def _target_bytes(real):
    return walk_stats(real)[1]


def _scan_cargo(proc_root):
    """First running cargo/rustc as 'comm (pid N)', else None. Unreadable entries are skipped."""
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "comm")) as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if comm in ("cargo", "rustc") or comm.startswith("cargo-"):
            return "%s (pid %s)" % (comm, pid)
    return None


def cargo_process_running():
    """Belt-and-braces for the cap (review of #1634): a test binary can run while cargo
    no longer holds .cargo-lock, so any live cargo/rustc on the host skips the clear."""
    return _scan_cargo("/proc")


def enforce_cap(real, profiles, max_gb, dry_run, lock_timeout, log, already_freed=0):
    """Whole-profile clear when over `max_gb` and every profile lock is free. See docstring."""
    cap_bytes = int(max_gb * (1 << 30))
    try:
        size = _target_bytes(real)
    except OSError as ex:
        # measured before any lock, i.e. while a build may churn files: unknown, not a crash
        log("SKIP cap: size walk failed (%s); nothing deleted" % ex)
        return {"status": "skipped-measure", "size": None, "cap": cap_bytes, "freed": 0}
    if dry_run:
        size = max(0, size - already_freed)
    out = {"status": "under-cap", "size": size, "cap": cap_bytes, "freed": 0}
    if size <= cap_bytes:
        log("cap: %d bytes <= %d, nothing to do" % (size, cap_bytes))
        return out
    busy = cargo_process_running()
    if busy:
        log("SKIP cap: %d bytes > %d but %s is running; nothing deleted" % (size, cap_bytes, busy))
        out["status"] = "skipped-busy"
        return out
    fds = []
    try:
        for prof in profiles:
            fd = acquire_lock(prof, lock_timeout)
            if fd is None:
                log("SKIP cap: %d bytes > %d but %s .cargo-lock not acquired (build running "
                    "or lock unopenable); nothing deleted" % (size, cap_bytes, prof))
                out["status"] = "skipped-locked"
                return out
            fds.append(fd)
        pre = "would clear" if dry_run else "clear"
        nested = {os.path.realpath(q) for q in profiles}
        freed = 0
        for prof in profiles:
            for e in _ls(prof):
                q = os.path.join(prof, e)
                if CARGO_LOCK_RE.match(e) or os.path.realpath(q) in nested:
                    continue
                try:
                    freed += walk_stats(q)[1]
                    log("cap: %s %s" % (pre, q))
                    if not dry_run:
                        safe_remove(q, prof)
                except OSError as ex:
                    out["errors"] = out.get("errors", 0) + 1
                    log("ERROR cap %s: %s (continuing)" % (q, ex))
        out["status"] = "would-clear" if dry_run else "cleared"
        out["freed"] = freed
        return out
    finally:
        for fd in fds:
            os.close(fd)


def prune_target(target, days=7.0, dry_run=False, lock_timeout=5.0, now=None, log=print,
                 superseded_hours=None, max_gb=None):
    if days < MIN_DAYS:
        raise Refused("refusing --days %s: minimum is %d" % (days, MIN_DAYS))
    if superseded_hours is not None and superseded_hours < MIN_SUPERSEDED_HOURS:
        raise Refused("refusing --superseded-hours %s: minimum is %d"
                      % (superseded_hours, MIN_SUPERSEDED_HOURS))
    real = check_scope(target)
    if not atime_supported(real):
        raise Refused("refusing %s: filesystem does not refresh atime (noatime?); last-use "
                      "staleness is unsafe and mtime-only would prune live dependencies" % target)
    if max_gb is not None and max_gb <= 0:
        raise Refused("refusing --max-gb %s: must be positive" % max_gb)
    profiles = find_profiles(real)
    results = Results(prune_profile(p, days, dry_run, lock_timeout, now, log, superseded_hours)
                      for p in profiles)
    if max_gb is not None:
        results.cap = enforce_cap(real, profiles, max_gb, dry_run, lock_timeout, log,
                                  already_freed=sum(r["freed"] for r in results))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Unit-granular prune of the shared echo cargo target.")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--lock-timeout", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--superseded-hours", type=float, default=None,
                    help="also prune units unused this long that a newer same-name unit "
                         "replaced (T-5249); off by default")
    ap.add_argument("--max-gb", type=float, default=None,
                    help="size cap (T-6121): when still over it after the normal prune and "
                         "every profile .cargo-lock is free, empty the profile dirs; off by default")
    a = ap.parse_args(argv)
    try:
        results = prune_target(a.target, a.days, a.dry_run, a.lock_timeout,
                               superseded_hours=a.superseded_hours, max_gb=a.max_gb)
    except Refused as e:
        print("echo_target_prune: %s" % e, file=sys.stderr)
        return 2
    verb = "would free" if a.dry_run else "freed"
    for r in results:
        print("%s: %s %d bytes, %d units, %d incremental dirs%s%s%s" % (
            r["profile"], verb, r["freed"], r["units"], r["incremental"],
            " SKIPPED(lock)" if r["skipped"] else "",
            " REPAIRED=%s" % r["repaired"] if r["repaired"] else "",
            " ERRORS=%d" % r["errors"] if r["errors"] else ""))
    print("TOTAL %s %d bytes" % (verb, sum(r["freed"] for r in results)))
    cap = results.cap
    if cap:
        size = "unknown" if cap["size"] is None else "%d bytes" % cap["size"]
        print("cap: %s (size %s, cap %d bytes, %s %d bytes)"
              % (cap["status"], size, cap["cap"], verb, cap["freed"]))
    if any(r["errors"] for r in results) or (cap and cap.get("errors")):
        return 3
    return 1 if any(r["repaired"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
