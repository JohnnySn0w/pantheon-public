"""Tests for tools/tmp_session_prune.py (T-6141) -- hermetic temp roots, utime ages,
a patchable proc root, and real `sleep` children for the cwd / open-fd cases."""
import contextlib
import io
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tools import tmp_session_prune as tsp

HOUR = 3600
PROJ = "-home-x-proj"
SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"
UID = os.getuid()


def age_tree(path, hours):
    """Set every entry under path (bottom-up, lstat-only) to `hours` old."""
    t = time.time() - hours * HOUR
    for dirpath, dirs, files in os.walk(path, topdown=False):
        for n in files + dirs:
            p = os.path.join(dirpath, n)
            os.utime(p, (t, t), follow_symlinks=False)
    os.utime(path, (t, t), follow_symlinks=False)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name
        self.root = os.path.join(self.tmp, "claude-root")
        self.projects = os.path.join(self.tmp, "claude-projects")
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.root)
        os.makedirs(self.projects)
        os.makedirs(self.proc)
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def session(self, sid=SID, age=48, transcript_age=48, files=("a.txt", "sub/b.txt"), project=PROJ):
        d = os.path.join(self.root, project, sid)
        for f in files:
            p = os.path.join(d, f)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write("data")
        age_tree(d, age)
        if transcript_age is not None:
            tdir = os.path.join(self.projects, project)
            os.makedirs(tdir, exist_ok=True)
            t = os.path.join(tdir, sid + ".jsonl")
            with open(t, "w") as fh:
                fh.write("{}")
            ts = time.time() - transcript_age * HOUR
            os.utime(t, (ts, ts))
        return d

    def run_sessions(self, dry_run=False, blob=b"", hours=24):
        return tsp.prune_sessions(self.root, self.projects, blob, hours, dry_run, log=self.log)

    # fake /proc helpers
    def fake_pid(self, pid, uid=UID, cwd=None, fds=(), maps="", cmdline=b"", environ=b""):
        p = os.path.join(self.proc, str(pid))
        os.makedirs(os.path.join(p, "fd"))
        with open(os.path.join(p, "status"), "w") as f:
            f.write("Name:\tx\nUid:\t%d\t%d\t%d\t%d\n" % (uid, uid, uid, uid))
        if cwd:
            os.symlink(cwd, os.path.join(p, "cwd"))
        for i, target in enumerate(fds):
            os.symlink(target, os.path.join(p, "fd", str(i)))
        with open(os.path.join(p, "maps"), "w") as f:
            f.write(maps)
        with open(os.path.join(p, "cmdline"), "wb") as f:
            f.write(cmdline)
        with open(os.path.join(p, "environ"), "wb") as f:
            f.write(environ)
        return p


class SessionAgeTests(Base):
    def test_old_dead_session_removed(self):
        d = self.session()
        res = self.run_sessions()
        self.assertFalse(os.path.exists(d))
        self.assertEqual(res["removed"], 1)
        self.assertEqual(res["errors"], 0)
        self.assertGreater(res["freed"], 0)

    def test_dry_run_deletes_nothing_and_reports(self):
        d = self.session()
        res = self.run_sessions(dry_run=True)
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(res["removed"], 1)
        self.assertTrue(any("would remove" in m and SID in m for m in self.logs))

    def test_recent_dir_kept(self):
        d = self.session(age=2)
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_newest_mtime_anywhere_in_tree_counts(self):
        d = self.session(age=48)
        now = time.time()
        os.utime(os.path.join(d, "sub", "b.txt"), (now, now))
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_boundary_just_over_24h_removed_just_under_kept(self):
        over = self.session(sid=SID, age=24.5)
        under = self.session(sid=SID2, age=23.5)
        self.run_sessions()
        self.assertFalse(os.path.exists(over))
        self.assertTrue(os.path.exists(under))

    def test_hours_floor_is_24(self):
        self.assertEqual(tsp.effective_hours(1), 24)
        self.assertEqual(tsp.effective_hours(72), 72)
        d = self.session(age=10)
        self.run_sessions(hours=1)
        self.assertTrue(os.path.isdir(d))


class SymlinkTests(Base):
    def test_symlinked_session_dir_never_followed_or_removed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("k")
        age_tree(outside, 100)
        os.makedirs(os.path.join(self.root, PROJ))
        link = os.path.join(self.root, PROJ, SID)
        os.symlink(outside, link)
        self.run_sessions()
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))

    def test_symlink_inside_tree_not_followed_on_delete(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "keep"), "w") as f:
            f.write("k")
        d = self.session()
        os.symlink(outside, os.path.join(d, "lnk"))
        age_tree(d, 48)
        self.run_sessions()
        self.assertFalse(os.path.exists(d))
        self.assertTrue(os.path.exists(os.path.join(outside, "keep")))

    def test_symlink_target_recency_does_not_count(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)  # fresh mtime
        d = self.session()
        os.symlink(outside, os.path.join(d, "lnk"))
        age_tree(d, 48)
        self.assertEqual(self.run_sessions()["removed"], 1)

    def test_symlinked_project_dir_ignored(self):
        outside = os.path.join(self.tmp, "outside-proj")
        victim = os.path.join(outside, SID)
        os.makedirs(victim)
        age_tree(victim, 100)
        os.symlink(outside, os.path.join(self.root, PROJ))
        self.run_sessions()
        self.assertTrue(os.path.isdir(victim))


class GitAndShapeTests(Base):
    def test_git_entry_keeps_dir(self):
        d = self.session(files=("a.txt", "repo/.git/HEAD"))
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_git_file_keeps_dir(self):
        d = self.session(files=("wt/.git",))
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_non_uuid_session_and_non_slug_project_ignored(self):
        a = self.session(sid="not-a-uuid")
        b = self.session(project="bundled-skills")
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(a) and os.path.isdir(b))

    def test_loose_files_in_root_ignored(self):
        f = os.path.join(self.root, "dbg.py")
        with open(f, "w") as fh:
            fh.write("x")
        age_tree(self.root, 100)
        self.run_sessions()
        self.assertTrue(os.path.exists(f))


class TranscriptTests(Base):
    def test_recent_transcript_keeps_dir(self):
        d = self.session(transcript_age=1)
        self.assertEqual(self.run_sessions()["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_old_transcript_removes(self):
        d = self.session(transcript_age=100)
        self.run_sessions()
        self.assertFalse(os.path.exists(d))

    def test_missing_transcript_counts_as_dead(self):
        d = self.session(transcript_age=None)
        self.run_sessions()
        self.assertFalse(os.path.exists(d))

    def test_unreadable_transcript_dir_keeps_and_counts_error(self):
        d = self.session()
        tdir = os.path.join(self.projects, PROJ)
        os.chmod(tdir, 0)
        self.addCleanup(os.chmod, tdir, 0o755)
        res = self.run_sessions()
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(res["errors"], 1)


class ProcScanTests(Base):
    def scan(self):
        return tsp.scan_proc(self.proc, UID)[0]

    def scan_denying_cwd(self, *pids):
        real = os.readlink

        def rl(path, *a, **k):
            if any(path == os.path.join(self.proc, str(p), "cwd") for p in pids):
                raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        with mock.patch.object(tsp.os, "readlink", rl):
            return tsp.scan_proc(self.proc, UID)

    def test_nondumpable_cmdline_clean_is_removable_and_counted(self):
        d = self.session()
        self.fake_pid(100, cwd=d, cmdline=b"gpg-agent\0--daemon\0")
        blob, n = self.scan_denying_cwd(100)
        self.assertEqual(n, 1)
        self.assertEqual(self.run_sessions(blob=blob)["removed"], 1)

    def test_nondumpable_cmdline_referencing_dir_keeps_it(self):
        d = self.session()
        self.fake_pid(100, cwd="/", cmdline=("sudo\0ls\0%s/a.txt\0" % d).encode())
        blob, n = self.scan_denying_cwd(100)
        self.assertEqual(n, 1)
        self.assertEqual(self.run_sessions(blob=blob)["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_nondumpable_only_cmdline_is_used(self):
        d = self.session()  # fd/environ would reference d, but are hidden by the signature
        self.fake_pid(100, cwd="/", fds=[d + "/a.txt"], environ=d.encode(), cmdline=b"x\0")
        blob, _ = self.scan_denying_cwd(100)
        self.assertNotIn(d.encode(), blob)

    def test_cwd_eacces_but_cmdline_unreadable_fails_closed(self):
        p = self.fake_pid(100, cwd="/")
        os.chmod(os.path.join(p, "cmdline"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "cmdline"), 0o644)
        with self.assertRaises(tsp.ProcScanError):
            self.scan_denying_cwd(100)

    def test_cwd_readable_but_fd_eacces_fails_closed(self):
        p = self.fake_pid(100, cwd="/")  # cwd fine -> NOT the tolerated signature
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)
        with self.assertRaises(tsp.ProcScanError):
            self.scan()

    def test_non_eacces_cwd_error_fails_closed(self):
        self.fake_pid(100, cwd="/")
        real = os.readlink

        def rl(path, *a, **k):
            if path.endswith("/100/cwd"):
                raise OSError(5, "io", path)
            return real(path, *a, **k)
        with mock.patch.object(tsp.os, "readlink", rl):
            with self.assertRaises(tsp.ProcScanError):
                tsp.scan_proc(self.proc, UID)

    def test_each_reference_kind_blocks_removal(self):
        d = self.session()
        kinds = {
            "cwd": dict(cwd=os.path.join(d, "sub")),
            "fd": dict(fds=[os.path.join(d, "a.txt")]),
            "maps": dict(maps="7f00-7f01 r-xp 0 08:01 1 %s/lib.so\n" % d),
            "cmdline": dict(cmdline=("tool\0--out\0%s/x\0" % d).encode()),
            "environ": dict(environ=("A=1\0TMPDIR=%s\0" % d).encode()),
        }
        for kind, kw in kinds.items():
            with self.subTest(kind=kind):
                import shutil
                shutil.rmtree(self.proc)
                os.makedirs(self.proc)
                self.fake_pid(100, **kw)
                res = self.run_sessions(blob=self.scan())
                self.assertEqual(res["removed"], 0)
                self.assertTrue(os.path.isdir(d))

    def test_unrelated_process_does_not_block(self):
        d = self.session()
        self.fake_pid(100, cwd="/", cmdline=b"bash\0", environ=b"A=1\0")
        self.assertEqual(self.run_sessions(blob=self.scan())["removed"], 1)
        self.assertFalse(os.path.exists(d))

    def test_other_uid_process_is_not_scanned_even_if_unreadable(self):
        d = self.session()
        p = self.fake_pid(100, uid=UID + 1, cwd=d)
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)
        blob = self.scan()  # must not raise
        self.assertEqual(self.run_sessions(blob=blob)["removed"], 1)

    def test_unreadable_own_uid_entry_fails_closed(self):
        p = self.fake_pid(100, cwd="/")
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)
        with self.assertRaises(tsp.ProcScanError):
            self.scan()

    def test_unreadable_environ_fails_closed(self):
        p = self.fake_pid(100, cwd="/")
        os.chmod(os.path.join(p, "environ"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "environ"), 0o644)
        with self.assertRaises(tsp.ProcScanError):
            self.scan()

    def test_vanished_process_is_tolerated(self):
        os.makedirs(os.path.join(self.proc, "100"))  # no status at all
        self.assertIsInstance(self.scan(), bytes)

    def test_non_numeric_entries_skipped(self):
        os.makedirs(os.path.join(self.proc, "self"))
        os.makedirs(os.path.join(self.proc, "sys"))
        self.assertIsInstance(self.scan(), bytes)

    def test_transient_fd_eacces_is_retried_and_tolerated(self):
        # T-6159: a same-uid process mid credential change (sudo) denies fd/ once
        d = self.session()
        self.fake_pid(100, cwd="/", fds=(os.path.join(d, "a.txt"),))
        real = os.listdir
        fd_dir = os.path.join(self.proc, "100", "fd")
        calls = []

        def ld(path, *a, **k):
            if path == fd_dir:
                calls.append(path)
                if len(calls) == 1:
                    raise PermissionError(13, "denied", path)
            return real(path, *a, **k)
        with mock.patch.object(tsp.os, "listdir", ld), mock.patch.object(tsp.time, "sleep"):
            blob = tsp.scan_proc(self.proc, UID)[0]
        self.assertEqual(len(calls), 2)
        self.assertIn(os.path.join(d, "a.txt").encode(), blob)

    def test_persistent_fd_eacces_still_fails_closed(self):
        p = self.fake_pid(100, cwd="/")
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)
        with mock.patch.object(tsp.time, "sleep"), self.assertRaises(tsp.ProcScanError):
            tsp.scan_proc(self.proc, UID)

    def test_pids_filter_scans_only_named_pids(self):
        p = self.fake_pid(100, cwd="/")
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)
        self.fake_pid(200, cwd="/srv/ok")
        blob = tsp.scan_proc(self.proc, UID, pids=(200,))[0]
        self.assertIn(b"/srv/ok", blob)

    def test_real_sleep_child_with_cwd_in_dir_blocks(self):
        d = self.session()
        child = subprocess.Popen(["sleep", "60"], cwd=os.path.join(d, "sub"))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        blob = tsp.scan_proc("/proc", UID, pids=(child.pid, os.getpid()))[0]
        res = self.run_sessions(blob=blob)
        self.assertEqual(res["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_real_sleep_child_with_open_fd_in_dir_blocks(self):
        d = self.session()
        fh = open(os.path.join(d, "a.txt"))
        self.addCleanup(fh.close)
        child = subprocess.Popen(["sleep", "60"], stdin=fh)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        fh.close()  # only the child still holds the file
        # T-6159: scan only this test's processes (unrelated same-uid host pids can EACCES)
        blob = tsp.scan_proc("/proc", UID, pids=(child.pid, os.getpid()))[0]
        res = self.run_sessions(blob=blob)
        self.assertEqual(res["removed"], 0)
        self.assertTrue(os.path.isdir(d))

    def test_real_scan_after_child_exits_allows_removal(self):
        d = self.session()
        child = subprocess.Popen(["sleep", "60"], cwd=d)
        child.kill()
        child.wait()
        blob = tsp.scan_proc("/proc", UID, pids=(child.pid, os.getpid()))[0]
        self.assertEqual(self.run_sessions(blob=blob)["removed"], 1)


class RootTests(Base):
    def test_refuses_root_owned_by_another_uid(self):
        with self.assertRaises(tsp.Refused):
            tsp.check_root(self.root, UID + 1)

    def test_accepts_own_root(self):
        tsp.check_root(self.root, UID)

    def test_refuses_symlink_root(self):
        link = os.path.join(self.tmp, "lnk")
        os.symlink(self.root, link)
        with self.assertRaises(tsp.Refused):
            tsp.check_root(link, UID)

    def test_default_root_is_per_uid(self):
        self.assertEqual(tsp.default_root(1234), "/tmp/claude-1234")

    def test_cli_refuses_foreign_root_exit_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = tsp.main(["--root", "/usr", "--dry-run"])
        self.assertEqual(rc, 2)


class ErrorTests(Base):
    def test_unwalkable_dir_is_counted_kept_and_run_continues(self):
        bad = self.session(sid=SID, files=("sub/b.txt",))
        locked = os.path.join(bad, "sub")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        good = self.session(sid=SID2)
        res = self.run_sessions()
        self.assertEqual(res["errors"], 1)
        self.assertTrue(os.path.isdir(bad))
        self.assertFalse(os.path.exists(good))
        self.assertEqual(res["removed"], 1)

    def test_main_exit_3_when_errors(self):
        bad = self.session(sid=SID, files=("sub/b.txt",))
        locked = os.path.join(bad, "sub")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tsp.main(["--root", self.root, "--claude-projects", self.projects], proc_root=self.proc)
        self.assertEqual(rc, 3)


class MainTests(Base):
    def main(self, *extra, proc_root=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = tsp.main(["--root", self.root, "--claude-projects", self.projects, *extra],
                          proc_root=self.proc if proc_root is None else proc_root)
        return rc, out.getvalue()

    def test_ok_exit_0_and_removes(self):
        d = self.session()
        rc, out = self.main()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(d))

    def test_dry_run_exit_0_nothing_removed(self):
        d = self.session()
        rc, out = self.main("--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(d))
        self.assertIn("would remove", out)

    def deny_fd(self, pid=100):
        p = self.fake_pid(pid, cwd="/")
        os.chmod(os.path.join(p, "fd"), 0)
        self.addCleanup(os.chmod, os.path.join(p, "fd"), 0o755)

    def test_scan_failure_deletes_nothing_skips_exit_0(self):
        # T-6159: a fail-closed scan is a SKIP (exit 0), not a unit failure
        d = self.session()
        self.deny_fd()
        state = os.path.join(self.tmp, "skips")
        rc, out = self.main("--state-file", state)
        self.assertEqual(rc, 0)
        self.assertIn("SKIP (proc scan failed closed, nothing deleted)", out)
        self.assertTrue(os.path.isdir(d))

    def test_third_consecutive_skip_exits_2(self):
        d = self.session()
        self.deny_fd()
        state = os.path.join(self.tmp, "skips")
        rcs = [self.main("--state-file", state)[0] for _ in range(3)]
        self.assertEqual(rcs, [0, 0, 2])
        self.assertTrue(os.path.isdir(d))

    def test_clean_run_resets_skip_count(self):
        self.session()
        state = os.path.join(self.tmp, "skips")
        with open(state, "w") as fh:
            fh.write("2\n")
        rc, _ = self.main("--state-file", state)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(state))

    def test_unwritable_skip_state_exits_2(self):
        # reviewer (T-6159): a counter that cannot be recorded must not SKIP silently forever
        d = self.session()
        self.deny_fd()
        blocker = os.path.join(self.tmp, "not-a-dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        rc, out = self.main("--state-file", os.path.join(blocker, "skips"))
        self.assertEqual(rc, 2)
        self.assertTrue(os.path.isdir(d))

    def test_uncleanable_skip_state_warns_not_crashes(self):
        d = self.session()
        sdir = os.path.join(self.tmp, "state")
        os.makedirs(sdir)
        state = os.path.join(sdir, "skips")
        with open(state, "w") as fh:
            fh.write("1\n")
        os.chmod(sdir, 0o500)  # the count file cannot be unlinked
        self.addCleanup(os.chmod, sdir, 0o755)
        rc, out = self.main("--state-file", state)
        self.assertEqual(rc, 0)
        self.assertIn("WARN", out)
        self.assertFalse(os.path.exists(d))

    def test_root_mode_without_state_file_writes_no_state(self):
        self.deny_fd()
        before = set(os.listdir(self.tmp))
        rcs = [self.main()[0] for _ in range(3)]
        self.assertEqual(rcs, [0, 0, 0])
        self.assertEqual(set(os.listdir(self.tmp)), before)

    def test_missing_root_is_a_clean_noop(self):
        os.rmdir(self.root)
        rc, _ = self.main()
        self.assertEqual(rc, 0)

    def test_pytest_not_touched_unless_base_given(self):
        base = os.path.join(self.tmp, "pytest-of-x")
        os.makedirs(os.path.join(base, "pytest-1"))
        age_tree(os.path.join(base, "pytest-1"), 100)
        self.main()
        self.assertTrue(os.path.isdir(os.path.join(base, "pytest-1")))


class PytestTests(Base):
    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.tmp, "pytest-of-x")
        os.makedirs(self.base)

    def mk(self, n, age):
        d = os.path.join(self.base, "pytest-%d" % n)
        os.makedirs(os.path.join(d, "t"))
        with open(os.path.join(d, "t", "f"), "w") as f:
            f.write("x")
        age_tree(d, age)
        return d

    def run_py(self, blob=b"", dry_run=False):
        return tsp.prune_pytest(self.base, UID, blob, 24, dry_run, log=self.log)

    def test_old_removed_recent_kept_current_target_kept(self):
        old, recent, cur = self.mk(1, 100), self.mk(2, 2), self.mk(3, 100)
        os.symlink(cur, os.path.join(self.base, "pytest-current"))
        res = self.run_py()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(recent))
        self.assertTrue(os.path.isdir(cur))
        self.assertTrue(os.path.islink(os.path.join(self.base, "pytest-current")))
        self.assertEqual(res["removed"], 1)

    def test_relative_current_target_kept(self):
        cur = self.mk(3, 100)
        os.symlink("pytest-3", os.path.join(self.base, "pytest-current"))
        self.run_py()
        self.assertTrue(os.path.isdir(cur))

    def test_other_names_and_symlinks_untouched(self):
        odd = os.path.join(self.base, "other")
        os.makedirs(odd)
        age_tree(odd, 100)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        age_tree(outside, 100)
        os.symlink(outside, os.path.join(self.base, "pytest-9"))
        self.run_py()
        self.assertTrue(os.path.isdir(odd))
        self.assertTrue(os.path.isdir(outside))
        self.assertTrue(os.path.islink(os.path.join(self.base, "pytest-9")))

    def test_referenced_by_process_kept(self):
        d = self.mk(1, 100)
        self.run_py(blob=d.encode() + b"/t\0")
        self.assertTrue(os.path.isdir(d))

    def test_dry_run(self):
        d = self.mk(1, 100)
        res = self.run_py(dry_run=True)
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(res["removed"], 1)

    def test_refuses_base_owned_by_other_uid(self):
        with self.assertRaises(tsp.Refused):
            tsp.prune_pytest(self.base, UID + 1, b"", 24, False, log=self.log)

    def test_missing_base_is_noop(self):
        res = tsp.prune_pytest(os.path.join(self.tmp, "nope"), UID, b"", 24, False, log=self.log)
        self.assertEqual(res["removed"], 0)

    def test_main_prunes_pytest_when_base_given(self):
        d = self.mk(1, 100)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = tsp.main(["--root", self.root, "--claude-projects", self.projects,
                           "--pytest-base", self.base], proc_root=self.proc)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(d))


if __name__ == "__main__":
    unittest.main()
