#!/usr/bin/env python3
"""tmp_session_prune.py -- owner-side prune of dead Claude Code session dirs in /tmp (T-6141).

INTENT. Claude Code leaves /tmp/claude-<uid>/<project>/<session-id>/ (scratchpads, task
output, tool results) behind forever; per-account these reached ~10G on 2026-11-03 and were
pruned by hand (T-6121). This runs per seat, as that seat's OWN account, from a systemd timer.
It never takes a cross-account argument: the root is /tmp/claude-$(os.getuid()).

A <project>/<session-id> dir is removed only if ALL of these hold:
  1. it is a real directory (lstat; a symlink is never followed or removed) and the NEWEST
     mtime anywhere in its tree is older than --hours (default and floor: 24);
  2. a /proc scan of the CALLER's own processes finds no cwd, fd, mmap (maps), cmdline or
     environ reference to the dir. An unreadable own-uid /proc entry is retried once (a
     process mid credential change, e.g. sudo, T-6159); if still unreadable the whole scan
     fails CLOSED and the run is a SKIP (nothing deleted), except non-dumpable processes
     (status+cmdline readable, cwd EACCES: systemd --user, gpg-agent, sudo), checked by
     cmdline only and counted in the run summary;
  3. the transcript ~/.claude/projects/<project>/<session-id>.jsonl is also older than
     --hours (a missing transcript counts as dead; an unreadable one keeps the dir);
  4. nothing named .git exists anywhere in the tree (a worktree or clone is never pruned).
Only names that look like Claude's own are considered: <project> starts with "-" (the slug
of an absolute path) and <session-id> is a UUID, so sibling dirs of the root are never touched.

Also /tmp/pytest-of-<user>/pytest-<N> dirs older than --hours, never the pytest-current
symlink's target, with the same proc and symlink rules. The pytest base is never pruned in
--root mode unless --pytest-base is also given.

--root / --pytest-base / --claude-projects exist for tests. --root is REFUSED unless it is a
real directory owned by the caller. No git writes, no commits.

EXIT CODES: 0 ok (including nothing to do, and a SKIP: the proc scan failed closed and nothing
was deleted), 2 refused (foreign-owned root, symlink root, or the SKIP_LIMIT-th consecutive
SKIP, so a persistent scan fault still alerts), 3 per-dir errors occurred (each is counted and
the run continues). Consecutive SKIPs are counted in ~/.local/state/pantheon/
tmp-session-prune.skips (removed on the next clean scan); a count that cannot be written exits
2 at once. --root mode counts nothing unless --state-file is given.
"""
import argparse
import os
import re
import shutil
import stat
import sys
import time

MIN_HOURS = 24
SKIP_LIMIT = 3  # T-6159: the 3rd consecutive fail-closed SKIP exits 2 (bounded, then alerts)
RETRY_PAUSE = 0.2  # seconds before re-reading a /proc entry that raised EACCES
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PYTEST_RE = re.compile(r"^pytest-[0-9]+$")


class Refused(Exception):
    pass


class ProcScanError(Exception):
    pass


def effective_hours(hours):
    return max(MIN_HOURS, hours)


def default_root(uid):
    return "/tmp/claude-%d" % uid


def check_root(root, uid):
    """Refuse unless root is a real directory (not a symlink) owned by uid."""
    try:
        st = os.lstat(root)
    except OSError as e:
        raise Refused("cannot stat root %s: %s" % (root, e))
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise Refused("root %s is not a real directory" % root)
    if st.st_uid != uid:
        raise Refused("root %s is owned by uid %d, not the caller (%d)" % (root, st.st_uid, uid))


def walk_tree(path):
    """(newest_mtime, bytes, has_git) over a tree by lstat only. Raises OSError on any
    unreadable directory: the caller keeps the dir, since it cannot be verified."""
    st = os.lstat(path)
    newest, total, git = st.st_mtime, 0, os.path.basename(path) == ".git"
    if not stat.S_ISDIR(st.st_mode):
        return newest, st.st_size, git
    with os.scandir(path) as it:
        entries = list(it)
    for e in entries:
        if e.name == ".git":
            git = True
        n, s, g = walk_tree(e.path)
        newest, total, git = max(newest, n), total + s, git or g
    return newest, total, git


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _own_uid(proc_root, pid, uid):
    """True/False for whether pid is the caller's; None if the process vanished."""
    try:
        text = _read(os.path.join(proc_root, pid, "status")).decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as e:
        raise ProcScanError("cannot read %s/%s/status: %s" % (proc_root, pid, e))
    for line in text.splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1]) == uid
            except (IndexError, ValueError):
                raise ProcScanError("unparseable Uid line for pid %s" % pid)
    return None if not text else _bad(pid)


def _bad(pid):
    raise ProcScanError("no Uid line in status of pid %s" % pid)


def scan_proc(proc_root, uid, pids=None):
    """(blob, n_nondumpable): one bytes blob of every cwd / fd / maps / cmdline / environ of the
    caller's own processes, and the count of NON-DUMPABLE-TOLERATED processes.

    Fail closed: any unreadable own-uid entry raises ProcScanError, EXCEPT the one
    ptrace-access signature (T-6141 ruling A): status and cmdline readable AND cwd raising
    EACCES. That is a non-dumpable same-uid process (systemd --user, gpg-agent, sudo): the
    kernel hides its cwd/fd/maps/environ from us, so only its cmdline is checked and it is
    counted. A process that exits mid-scan (ENOENT/ESRCH) contributes nothing. Any other
    unreadable entry is retried once after RETRY_PAUSE (T-6159: a same-uid process mid
    credential change, e.g. sudo, briefly denies fd/) before failing closed.

    `pids` (tests only) limits the scan to those pids; production scans every pid."""
    parts = []
    nondump = 0
    if pids is not None:
        names = [str(p) for p in pids]
    else:
        try:
            names = [p for p in os.listdir(proc_root) if p.isdigit()]
        except OSError as e:
            raise ProcScanError("cannot list %s: %s" % (proc_root, e))
    for pid in names:
        own = _own_uid(proc_root, pid, uid)
        if not own:
            continue
        base = os.path.join(proc_root, pid)
        try:
            got = _scan_pid(base)
        except OSError:
            time.sleep(RETRY_PAUSE)
            try:
                got = _scan_pid(base)
            except OSError as e:
                if os.path.exists(os.path.join(base, "status")):
                    raise ProcScanError("unreadable own-uid /proc entry %s: %s" % (base, e))
                continue
        if got is None:
            continue
        pid_parts, is_nondump = got
        parts.extend(pid_parts)
        nondump += is_nondump
    return b"\0".join(parts), nondump


def _scan_pid(base):
    """(parts, 1 if non-dumpable else 0) for one own-uid pid. Raises OSError when an entry is
    unreadable for any reason other than the process having gone."""
    gone = (FileNotFoundError, ProcessLookupError)
    parts = []
    try:
        cwd = os.readlink(os.path.join(base, "cwd")).encode("utf-8", "surrogateescape")
    except gone:
        cwd = None
    except PermissionError:
        # ptrace-access signature: tolerated only if cmdline is readable
        return [_read(os.path.join(base, "cmdline"))], 1
    if cwd is not None:
        parts.append(cwd)
    try:
        fds = os.listdir(os.path.join(base, "fd"))
    except gone:
        fds = []
    for fd in fds:
        try:
            parts.append(os.readlink(os.path.join(base, "fd", fd)).encode("utf-8", "surrogateescape"))
        except gone:
            pass
    for name in ("maps", "cmdline", "environ"):
        try:
            parts.append(_read(os.path.join(base, name)))
        except gone:
            pass
    return parts, 0


def referenced(blob, path):
    return os.fsencode(path) in blob


def _transcript_recent(projects_root, project, sid, cutoff):
    """True if a transcript exists and is newer than cutoff. Missing -> False.
    Raises OSError for anything but missing."""
    try:
        st = os.lstat(os.path.join(projects_root, project, sid + ".jsonl"))
    except FileNotFoundError:
        return False
    return st.st_mtime > cutoff


def _real_dirs(path):
    """Names of real (non-symlink) subdirs of path; [] if it cannot be listed."""
    try:
        with os.scandir(path) as it:
            return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _remove(path, size, dry_run, log, res):
    log("%s%s %d bytes" % ("would remove " if dry_run else "remove ", path, size))
    if not dry_run:
        shutil.rmtree(path)
    res["removed"] += 1
    res["freed"] += size


def prune_sessions(root, projects_root, blob, hours, dry_run, now=None, log=print):
    now = time.time() if now is None else now
    cutoff = now - effective_hours(hours) * 3600
    res = {"removed": 0, "freed": 0, "kept": 0, "errors": 0}
    for project in _real_dirs(root):
        if not project.startswith("-"):
            continue
        pdir = os.path.join(root, project)
        for sid in _real_dirs(pdir):
            if not SESSION_RE.match(sid):
                continue
            d = os.path.join(pdir, sid)
            try:
                newest, size, git = walk_tree(d)
                if newest > cutoff or git or referenced(blob, d):
                    res["kept"] += 1
                    continue
                if _transcript_recent(projects_root, project, sid, cutoff):
                    res["kept"] += 1
                    continue
                _remove(d, size, dry_run, log, res)
            except OSError as e:
                res["errors"] += 1
                log("ERROR %s: %s (continuing)" % (d, e))
    return res


def prune_pytest(base, uid, blob, hours, dry_run, now=None, log=print):
    now = time.time() if now is None else now
    cutoff = now - effective_hours(hours) * 3600
    res = {"removed": 0, "freed": 0, "kept": 0, "errors": 0}
    if not os.path.lexists(base):
        return res
    check_root(base, uid)
    current = None
    try:
        current = os.path.realpath(os.path.join(base, os.readlink(os.path.join(base, "pytest-current"))))
    except FileNotFoundError:
        pass
    except OSError as e:
        res["errors"] += 1
        log("ERROR %s/pytest-current: %s (skipping pytest prune)" % (base, e))
        return res
    for name in _real_dirs(base):
        if not PYTEST_RE.match(name):
            continue
        d = os.path.join(base, name)
        if current is not None and os.path.realpath(d) == current:
            res["kept"] += 1
            continue
        try:
            newest, size, _git = walk_tree(d)
            if newest > cutoff or referenced(blob, d):
                res["kept"] += 1
                continue
            _remove(d, size, dry_run, log, res)
        except OSError as e:
            res["errors"] += 1
            log("ERROR %s: %s (continuing)" % (d, e))
    return res


def main(argv=None, proc_root="/proc"):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=float, default=MIN_HOURS, help="age floor in hours (min 24)")
    ap.add_argument("--root", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--pytest-base", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--claude-projects", help=argparse.SUPPRESS)  # tests only
    ap.add_argument("--state-file", help=argparse.SUPPRESS)  # tests only (skip counter)
    args = ap.parse_args(argv)
    uid = os.getuid()
    root = args.root or default_root(uid)
    projects = args.claude_projects or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if args.pytest_base:
        pbase = args.pytest_base
    elif args.root:
        pbase = None
    else:
        import pwd
        pbase = "/tmp/pytest-of-" + pwd.getpwuid(uid).pw_name
    if args.state_file:
        state = args.state_file
    elif args.root:
        state = None  # --root (test) mode never touches the account's real state
    else:
        state = os.path.join(os.path.expanduser("~"), ".local", "state", "pantheon",
                             "tmp-session-prune.skips")
    totals = {"removed": 0, "freed": 0, "kept": 0, "errors": 0}
    try:
        if os.path.lexists(root):
            check_root(root, uid)
        elif not pbase:
            print("nothing to do: %s does not exist" % root)
            return 0
        blob, nondump = scan_proc(proc_root, uid)
        results = []
        if os.path.lexists(root):
            results.append(prune_sessions(root, projects, blob, args.hours, args.dry_run))
        if pbase:
            results.append(prune_pytest(pbase, uid, blob, args.hours, args.dry_run))
    except Refused as e:
        print("REFUSED: %s" % e, file=sys.stderr)
        return 2
    except ProcScanError as e:
        n = _bump_skips(state)
        print("SKIP (proc scan failed closed, nothing deleted): %s [consecutive skips: %s]"
              % (e, n if state else "not counted"))
        if state and n >= SKIP_LIMIT:
            print("REFUSED: %d consecutive fail-closed proc scans (limit %d)" % (n, SKIP_LIMIT),
                  file=sys.stderr)
            return 2
        return 0
    _clear_skips(state)
    for r in results:
        for k in totals:
            totals[k] += r[k]
    print("%d non-dumpable processes checked by cmdline only" % nondump)
    print("%s: %d dir(s), %d bytes, kept %d, errors %d" % (
        "would remove" if args.dry_run else "removed",
        totals["removed"], totals["freed"], totals["kept"], totals["errors"]))
    return 3 if totals["errors"] else 0


def _bump_skips(state):
    if not state:
        return 0
    try:
        with open(state) as fh:
            n = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    n += 1
    try:
        os.makedirs(os.path.dirname(state), exist_ok=True)
        tmp = state + ".tmp"
        with open(tmp, "w") as fh:
            fh.write("%d\n" % n)
        os.replace(tmp, state)
    except OSError as e:
        # an unrecordable count must not SKIP silently forever: escalate now (exit 2)
        print("WARN: could not record the skip count in %s: %s" % (state, e), file=sys.stderr)
        return SKIP_LIMIT
    return n


def _clear_skips(state):
    if state:
        try:
            os.unlink(state)
        except FileNotFoundError:
            pass
        except OSError as e:
            print("WARN: could not clear the skip count %s: %s" % (state, e), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
