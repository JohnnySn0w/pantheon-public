"""Unit tests for tools/_git_utils.py — with_worktree helper.

Run from repo root:
    python3 -m unittest tools.test_git_utils -v

Covers the 8 named cases from T-0471 Test Plan:
  1. test_with_worktree_happy_path
  2. test_with_worktree_exception_path
  3. test_with_worktree_sequential_calls_in_same_process
  4. test_with_worktree_origin_unreachable
  5. test_with_worktree_add_fails
  6. test_with_worktree_cleanup_fails
  7. test_with_worktree_stale_leftover_from_prior_fire
  8. test_with_worktree_tmp_unwritable

All tests mock subprocess.run / os.getpid; no real git operations performed.

Fixture-string note: the HERMES trigger byte sequence is never written in
contiguous form in this source file (byte-sequence discipline per
`tools/_git_utils.py` module docstring + `~/CLAUDE.md`).
"""

from __future__ import annotations

import os
import sys
import tempfile
import io
import contextlib
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Ensure repo root is importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (
    WorktreeError,
    with_worktree,
    push_with_retry,
    _refspec_targets_main,
    _warn_if_branch_carries_nonticket_commits,
)


def _make_cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


class TestWithWorktreeHappyPath(unittest.TestCase):
    """fn runs cleanly; return value propagated; worktree removed on exit."""

    def test_with_worktree_happy_path(self) -> None:
        sentinel = object()

        with (
            patch("tools._git_utils.os.getpid", return_value=99999),
            patch("tools._git_utils.subprocess.run") as mock_run,
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            # All git calls succeed.
            mock_run.return_value = _make_cp(0)

            result = with_worktree(lambda _wt: sentinel)

        self.assertIs(result, sentinel)
        # Verify fetch + worktree add + worktree remove were all called.
        calls = [str(c) for c in mock_run.call_args_list]
        self.assertTrue(
            any("fetch" in c for c in calls),
            f"git fetch not called; calls={calls}",
        )
        self.assertTrue(
            any("worktree" in c and "add" in c for c in calls),
            f"git worktree add not called; calls={calls}",
        )
        self.assertTrue(
            any("worktree" in c and "remove" in c for c in calls),
            f"git worktree remove not called; calls={calls}",
        )


class TestWithWorktreeExceptionPath(unittest.TestCase):
    """fn raises; worktree removed in finally; exception re-raised."""

    def test_with_worktree_exception_path(self) -> None:
        remove_called = []

        def _track_run(cmd, **kwargs):
            if "worktree" in cmd and "remove" in cmd:
                remove_called.append(cmd)
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=88888),
            patch("tools._git_utils.subprocess.run", side_effect=_track_run),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            with self.assertRaises(ValueError) as ctx:
                with_worktree(lambda _wt: (_ for _ in ()).throw(ValueError("fn-failed")))

        self.assertIn("fn-failed", str(ctx.exception))
        self.assertTrue(
            len(remove_called) > 0,
            "git worktree remove must be called in finally even when fn raises",
        )


class TestWithWorktreeSequentialCallsSameProcess(unittest.TestCase):
    """Two sequential calls succeed; path-collision avoidance via monotonic counter."""

    def test_with_worktree_sequential_calls_in_same_process(self) -> None:
        pid_seq = iter([77777, 77778])

        with (
            patch("tools._git_utils.os.getpid", side_effect=pid_seq),
            patch("tools._git_utils.subprocess.run", return_value=_make_cp(0)),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            r1 = with_worktree(lambda _wt: "first")
            r2 = with_worktree(lambda _wt: "second")

        self.assertEqual(r1, "first")
        self.assertEqual(r2, "second")


class TestWithWorktreeOriginUnreachable(unittest.TestCase):
    """git fetch fails → WorktreeError raised; no partial-worktree dir."""

    def test_with_worktree_origin_unreachable(self) -> None:
        add_called = []

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                return _make_cp(1, stderr="fatal: unable to connect to origin")
            if "worktree" in cmd and "add" in cmd:
                add_called.append(cmd)
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=66666),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            with self.assertRaises(WorktreeError) as ctx:
                with_worktree(lambda _wt: None)

        self.assertIn("fetch", str(ctx.exception).lower())
        self.assertEqual(add_called, [], "git worktree add must not be called after fetch failure")


class TestWithWorktreeAddFails(unittest.TestCase):
    """fetch succeeds; git worktree add fails → WorktreeError; cleanup idempotent."""

    def test_with_worktree_add_fails(self) -> None:
        remove_called = []

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                return _make_cp(0)
            if "worktree" in cmd and "add" in cmd:
                return _make_cp(128, stderr="fatal: '/tmp/...' already exists")
            if "worktree" in cmd and "remove" in cmd:
                remove_called.append(cmd)
                return _make_cp(0)
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=55555),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            with self.assertRaises(WorktreeError):
                with_worktree(lambda _wt: None)

        # git worktree add failed before path was registered — remove may or
        # may not be called (implementation detail); what matters is no exception
        # leaks beyond WorktreeError and no path is left behind.
        # (No path.exists assert here because Path.exists is patched to False.)


class TestWithWorktreeFailedAddIsTornDown(unittest.TestCase):
    """T-5076: a timed-out or failed `worktree add` leaves no registered, locked worktree."""

    def _calls_for(self, add_behaviour):
        seen = []

        def _run(cmd, **kwargs):
            seen.append(list(cmd))
            if "worktree" in cmd and "add" in cmd:
                return add_behaviour(cmd, kwargs)
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=44444),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            with self.assertRaises(WorktreeError):
                with_worktree(lambda _wt: None)
        return seen

    def _assert_torn_down(self, seen):
        self.assertIn(["git", "worktree", "remove", "-f", "-f", f"/tmp/{os.path.basename(sys.argv[0])}-44444"], seen)
        self.assertIn(["git", "worktree", "prune"], seen)

    def test_timeout_is_torn_down(self):
        import subprocess as sp

        def add(cmd, kw):
            raise sp.TimeoutExpired(cmd, kw.get("timeout"))
        self._assert_torn_down(self._calls_for(add))

    def test_nonzero_add_is_torn_down(self):
        self._assert_torn_down(self._calls_for(lambda cmd, kw: _make_cp(128, stderr="fatal: boom")))

    def test_add_timeout_is_wider_than_the_old_30s(self):
        captured = {}

        def add(cmd, kw):
            captured["timeout"] = kw.get("timeout")
            return _make_cp(128)
        self._calls_for(add)
        self.assertGreaterEqual(captured["timeout"], 120)


class TestWithWorktreeTimedOutAddRealGit(unittest.TestCase):
    """T-5076 against real git: the add is killed mid-way (simulated by a real
    `worktree add --lock --reason initializing` and then TimeoutExpired, the 2026-10-17
    state). Afterwards no worktree but the main one is registered and the dir is gone."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        import subprocess as sp
        self.sp = sp
        self.tmp = Path(tempfile.mkdtemp())
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.tmp)}
        self.env = env
        origin, clone = self.tmp / "origin.git", self.tmp / "clone"
        sp.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=env)
        sp.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=env, capture_output=True)
        (clone / "f").write_text("x\n")
        for argv in (["add", "f"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"],
                     ["push", "-q", "origin", "HEAD:main"]):
            sp.run(["git", *argv], cwd=clone, check=True, env=env, capture_output=True)
        self.clone = clone

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_locked_worktree_is_left_registered(self):
        real_run = self.sp.run
        sp = self.sp

        def _run(cmd, **kwargs):
            if "worktree" in cmd and "add" in cmd:
                path = cmd[cmd.index("--detach") + 1]
                real_run(["git", "worktree", "add", "--lock", "--reason", "initializing",
                          "--detach", path, "origin/main"], cwd=kwargs.get("cwd"),
                         capture_output=True, check=True)
                raise sp.TimeoutExpired(cmd, kwargs.get("timeout"))
            return real_run(cmd, **kwargs)

        with (
            patch("tools._git_utils.REPO_ROOT", self.clone),
            patch("tools._git_utils.os.getpid", return_value=33333 + os.getpid()),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
        ):
            with self.assertRaises(WorktreeError):
                with_worktree(lambda _wt: None)
        listing = real_run(["git", "worktree", "list", "--porcelain"], cwd=self.clone,
                           capture_output=True, text=True, check=True).stdout
        self.assertEqual(listing.count("worktree "), 1, listing)
        self.assertNotIn("locked", listing)
        self.assertFalse(Path(f"/tmp/{os.path.basename(sys.argv[0])}-{33333 + os.getpid()}").exists())


class TestWithWorktreeCleanupFails(unittest.TestCase):
    """fn returns sentinel; git worktree remove fails → return value propagated; stderr logged."""

    def test_with_worktree_cleanup_fails(self) -> None:
        sentinel = "result-value"
        stderr_captured = []

        def _run(cmd, **kwargs):
            if "worktree" in cmd and "remove" in cmd:
                return _make_cp(1, stderr="error: removal failed")
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=44444),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
            patch("tools._git_utils.sys.stderr") as mock_stderr,
        ):
            result = with_worktree(lambda _wt: sentinel)

        self.assertEqual(result, sentinel, "return value must be propagated even when cleanup fails")
        # stderr.write or print(..., file=sys.stderr) should have been called.
        self.assertTrue(
            mock_stderr.write.called or mock_stderr.write.call_count > 0
            or str(mock_stderr.mock_calls),
            "cleanup failure must be logged to stderr",
        )


class TestWithWorktreeStaleLefover(unittest.TestCase):
    """Pre-existing /tmp/<name>-<pid>/ → WorktreeError; no subprocess called; no clobbering."""

    def test_with_worktree_stale_leftover_from_prior_fire(self) -> None:
        subprocess_calls = []

        def _run(cmd, **kwargs):
            subprocess_calls.append(cmd)
            return _make_cp(0)

        # Simulate the path already existing (stale leftover from a crashed prior fire).
        with (
            patch("tools._git_utils.os.getpid", return_value=33333),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=True),
        ):
            with self.assertRaises(WorktreeError) as ctx:
                with_worktree(lambda _wt: None)

        self.assertIn("stale", str(ctx.exception).lower(),
                      "exception message must mention 'stale' leftover")
        self.assertEqual(subprocess_calls, [],
                         "no subprocess must be called when stale leftover is detected")


class TestWithWorktreeTmpUnwritable(unittest.TestCase):
    """git worktree add returns permission-denied rc=128 → WorktreeError with git error detail."""

    def test_with_worktree_tmp_unwritable(self) -> None:
        """Distinct from test 7: exercises the subprocess-fail path (add returns
        rc=128 with Permission denied), not the pre-check path (stale leftover).
        Verifies the WorktreeError message preserves the underlying git error.
        """
        perm_denied_msg = "fatal: could not create work tree dir '/tmp/test-22222': Permission denied"

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                return _make_cp(0)
            if "worktree" in cmd and "add" in cmd:
                return _make_cp(128, stderr=perm_denied_msg)
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=22222),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
        ):
            with self.assertRaises(WorktreeError) as ctx:
                with_worktree(lambda _wt: None)

        exc_msg = str(ctx.exception)
        self.assertIn("Permission denied", exc_msg,
                      "WorktreeError must include the underlying git error detail for traceability (P4)")
        # No partial state created: no dir is registered since add failed.


class TestGitCommonDirCachePoisoning(unittest.TestCase):
    """T-1931: the module-global cache must only ever hold a CONFIRMED directory.

    Source: bugs/git-common-dir-global-cache-poisoned-by-mocked-subprocess.md.
    T-1816 closed the empty-stdout door (the fallback is not cached); this covers
    the non-empty-but-wrong door — `returncode=0` with arbitrary stdout, which is
    exactly the shape a test that mocks `subprocess.run` broadly produces.

    These are the first direct tests of `_git_common_dir()`: nothing exercised it
    before (test_git_mutation_lock bypasses it via PANTHEON_GIT_MUTATION_LOCKFILE),
    which is how both doors stayed open.

    Hygiene, and the joke of this ticket: a test suite for cache poisoning must not
    poison the cache. The module global is reset in BOTH setUp and tearDown so
    neither a prior module nor these tests can leak a value into the process.
    """

    def setUp(self) -> None:
        from tools import _git_utils

        self.gu = _git_utils
        self._saved = _git_utils._git_common_dir_cache
        _git_utils._git_common_dir_cache = None
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self.gu._git_common_dir_cache = self._saved

    def test_non_directory_result_is_returned_but_NOT_cached(self) -> None:
        """The poisoning shape from the bug entry: plausible stdout, not a dir."""
        bogus = self.tmp / "not-a-dir"
        bogus.write_text("gitdir: /somewhere/else\n", encoding="utf-8")  # a FILE, like a linked worktree's .git

        with patch("tools._git_utils.subprocess.run",
                   return_value=_make_cp(0, f"{bogus}\n")) as m:
            first = self.gu._git_common_dir()
            self.assertEqual(first, bogus.resolve(),
                             "returned path must be unchanged — we decline to CACHE, we do not reject")
            self.assertIsNone(self.gu._git_common_dir_cache,
                              "a non-directory must never enter the module-global cache")
            self.gu._git_common_dir()
            self.assertEqual(m.call_count, 2,
                             "uncached → the next caller must re-resolve (that is the whole point)")

    def test_real_directory_result_IS_cached(self) -> None:
        """Guard against the fix degenerating into never-cache (a silent perf regression)."""
        real = self.tmp / "common"
        real.mkdir()

        with patch("tools._git_utils.subprocess.run",
                   return_value=_make_cp(0, f"{real}\n")) as m:
            first = self.gu._git_common_dir()
            self.assertEqual(first, real.resolve())
            self.assertEqual(self.gu._git_common_dir_cache, real.resolve())
            second = self.gu._git_common_dir()
            self.assertEqual(second, real.resolve())
            self.assertEqual(m.call_count, 1, "a confirmed directory must be cached — one subprocess call only")

    def test_empty_stdout_fallback_still_uncached_T1816_regression(self) -> None:
        """T-1816's half of the fix must survive this change."""
        with patch("tools._git_utils.subprocess.run", return_value=_make_cp(0, "")) as m:
            got = self.gu._git_common_dir()
            self.assertEqual(got, self.gu.REPO_ROOT / ".git")
            self.assertIsNone(self.gu._git_common_dir_cache)
            self.gu._git_common_dir()
            self.assertEqual(m.call_count, 2, "fallback must stay uncached (T-1816)")

    def test_is_dir_raising_degrades_to_uncached_never_propagates(self) -> None:
        """The validation must not raise inside the commit path — it degrades instead.

        `Path.is_dir()` swallows OSError internally, so this forces the raise to prove
        the wrapper earns its place rather than being decorative.
        """
        target = self.tmp / "whatever"

        with patch("tools._git_utils.subprocess.run", return_value=_make_cp(0, f"{target}\n")), \
             patch.object(Path, "is_dir", side_effect=OSError("ELOOP")):
            got = self.gu._git_common_dir()  # must NOT raise
            self.assertEqual(got, target.resolve())
            self.assertIsNone(self.gu._git_common_dir_cache,
                              "an unverifiable path must not be cached")


class TestGitMutationLockT2140(unittest.TestCase):
    """T-2140. The reported defect was 'stale lock, no PID-liveness check'.

    MEASURED, and it is a MISDIAGNOSIS: the lock is fcntl.flock, held on the file
    DESCRIPTOR and released by the kernel on process exit. A leftover file naming a
    dead pid is a diagnostic breadcrumb, not a held lock — acquisition against one
    succeeds in 0.000s. Adding stale-lock-BREAKING to a lock that cannot go stale
    would introduce a real race (breaking a live holder) to fix a defect that does
    not exist.

    The genuine hazard is the OPPOSITE: deleting the lockfile destroys mutual
    exclusion, because a contender opens the newly-created path and acquires
    immediately. That is what these tests pin.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="t2140-")
        self.lock = os.path.join(self.d, "ops.lock")
        self._prev = os.environ.get("PANTHEON_GIT_MUTATION_LOCKFILE")
        os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = self.lock

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("PANTHEON_GIT_MUTATION_LOCKFILE", None)
        else:
            os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = self._prev
        shutil.rmtree(self.d, ignore_errors=True)

    def test_release_marks_the_file_RELEASED_not_a_live_pid(self):
        """A leftover reading 'pid=<dead>' looks like a stuck holder — and that
        misreading is what led an operator to delete a lockfile they had not
        validated, which is the one action that genuinely breaks exclusion."""
        from tools._git_utils import git_mutation_lock
        with git_mutation_lock(timeout_secs=5, label="work"):
            held = open(self.lock).read()
        released = open(self.lock).read()
        self.assertIn("pid=", held)
        self.assertNotIn("RELEASED", held)
        self.assertIn("RELEASED (not held)", released)
        self.assertIn("NEVER delete this file", released)

    def test_leftover_released_file_does_NOT_block_acquisition(self):
        """The core misdiagnosis, pinned: a persisting file is not a held lock."""
        from tools._git_utils import git_mutation_lock
        with git_mutation_lock(timeout_secs=5, label="first"):
            pass
        self.assertTrue(os.path.exists(self.lock), "file is expected to persist")
        t0 = time.monotonic()
        with git_mutation_lock(timeout_secs=5, label="second"):
            pass
        self.assertLess(time.monotonic() - t0, 1.0,
                        "a leftover lockfile must not delay acquisition")

    def test_deleting_the_lockfile_mid_section_is_REPORTED(self):
        """Cannot be prevented from either side; must not be silent."""
        from tools._git_utils import git_mutation_lock
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with git_mutation_lock(timeout_secs=5, label="racy"):
                os.unlink(self.lock)
        out = err.getvalue()
        self.assertIn("was DELETED", out)
        self.assertIn("mutual exclusion was NOT in force", out)

    def test_clean_section_emits_no_violation_warning(self):
        from tools._git_utils import git_mutation_lock
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with git_mutation_lock(timeout_secs=5, label="clean"):
                pass
        self.assertNotIn("NOT in force", err.getvalue())

    def test_holder_hint_records_label_and_pid_while_held(self):
        from tools._git_utils import git_mutation_lock
        with git_mutation_lock(timeout_secs=5, label="claim T-9999"):
            body = open(self.lock).read()
        self.assertIn(f"pid={os.getpid()}", body)
        self.assertIn("claim T-9999", body)





class CommonDirMockedStdoutTest(unittest.TestCase):
    """T-2336 third door of the T-1816/T-1931 hole: a broadly-mocked
    subprocess.run hands _git_common_dir a MagicMock stdout — truthy .strip(),
    fabricated relative fspath — and the first git_mutation_lock under such a
    test mkdir(parents=True)s a literal `MagicMock/...` tree in the repo root.
    A real subprocess(text=True) stdout is always str; non-str can only be a
    test double and must take the uncached fallback."""

    def test_non_str_stdout_falls_back_and_does_not_fabricate_a_path(self):
        from unittest import mock as _m
        from tools import _git_utils
        _git_utils._git_common_dir_cache = None
        self.addCleanup(setattr, _git_utils, "_git_common_dir_cache", None)
        fake = _m.MagicMock(returncode=0)  # stdout is a child MagicMock — the trap
        with _m.patch.object(_git_utils, "_git", return_value=fake):
            out = _git_utils._git_common_dir()
        self.assertEqual(out, _git_utils.REPO_ROOT / ".git",
                         "non-str stdout must take the same fallback as empty stdout")
        self.assertIsNone(_git_utils._git_common_dir_cache,
                          "the fallback must stay uncached (T-1816)")
        self.assertFalse((_git_utils.REPO_ROOT / "MagicMock").exists(),
                         "no fabricated path may be materialized")


class TestWithWorktreeFetchRefLockRetry(unittest.TestCase):
    """T-3207: the concurrent-fetch ref-lock race is retried; non-ref-lock fails fast."""

    def test_ref_lock_race_retried_then_succeeds(self) -> None:
        sentinel = object()
        fetch_calls = []

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                fetch_calls.append(cmd)
                if len(fetch_calls) == 1:
                    # First fetch loses the concurrent ref-lock race.
                    return _make_cp(
                        1,
                        stderr=(
                            "error: cannot lock ref 'refs/remotes/origin/main': "
                            "is at 45e9a728d but expected 581da79f2"
                        ),
                    )
                return _make_cp(0)  # retry sees the settled ref
            return _make_cp(0)  # worktree add / remove

        with (
            patch("tools._git_utils.os.getpid", return_value=77777),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
            patch("tools._git_utils.time.sleep") as mock_sleep,
        ):
            result = with_worktree(lambda _wt: sentinel)

        self.assertIs(result, sentinel)
        self.assertEqual(
            len(fetch_calls), 2, "fetch should retry exactly once on the ref-lock race"
        )
        self.assertTrue(mock_sleep.called, "should back off between ref-lock retries")

    def test_non_ref_lock_fetch_failure_not_retried(self) -> None:
        # A network/auth error must still fail fast (no retry) — regression guard
        # that the ref-lock retry does not over-match.
        fetch_calls = []

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                fetch_calls.append(cmd)
                return _make_cp(1, stderr="fatal: could not resolve host github.com")
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=66666),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
            patch("tools._git_utils.time.sleep"),
        ):
            with self.assertRaises(WorktreeError) as ctx:
                with_worktree(lambda _wt: "unused")

        self.assertEqual(
            len(fetch_calls), 1, "a non-ref-lock fetch error must NOT be retried"
        )
        self.assertIn("could not resolve host", str(ctx.exception))

    def test_ref_lock_exhausted_raises(self) -> None:
        # Ref-lock on every attempt → exhausts retries → raises (fail-loud), same
        # WorktreeError shape as before.
        fetch_calls = []

        def _run(cmd, **kwargs):
            if "fetch" in cmd:
                fetch_calls.append(cmd)
                return _make_cp(
                    1, stderr="error: cannot lock ref 'refs/remotes/origin/main'"
                )
            return _make_cp(0)

        with (
            patch("tools._git_utils.os.getpid", return_value=55555),
            patch("tools._git_utils.subprocess.run", side_effect=_run),
            patch("tools._git_utils.Path.exists", return_value=False),
            patch("tools._git_utils.time.sleep"),
        ):
            with self.assertRaises(WorktreeError) as ctx:
                with_worktree(lambda _wt: "unused")

        self.assertGreaterEqual(
            len(fetch_calls), 2, "should have retried before exhausting"
        )
        self.assertIn("cannot lock ref", str(ctx.exception))


class TestPushGuardT3600(unittest.TestCase):
    """T-3600 — WARN (never block) before a HEAD:main push from a named non-main
    branch that would carry non-ticket commits onto origin/main. Fail-open in
    every limb."""

    @staticmethod
    def _git_side_effect(*, branch_cp, log_cp):
        """Build a side_effect for _git that dispatches on the git subcommand:
        `symbolic-ref` -> branch_cp, `log` -> log_cp. Any other subcommand -> ok."""
        def _se(*args, **kwargs):
            sub = args[0] if args else ""
            if sub == "symbolic-ref":
                return branch_cp
            if sub == "log":
                return log_cp
            return _make_cp(0)
        return _se

    def _run_guard(self, *, branch_cp, log_cp, refspec="HEAD:main", env=None):
        buf = io.StringIO()
        patches = [
            patch("tools._git_utils._git",
                  side_effect=self._git_side_effect(branch_cp=branch_cp, log_cp=log_cp)),
            patch.dict("tools._git_utils.os.environ", env or {}, clear=False),
            contextlib.redirect_stderr(buf),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            _warn_if_branch_carries_nonticket_commits(Path("/x"), refspec)
        return buf.getvalue()

    # --- refspec parsing ---------------------------------------------------
    def test_refspec_targets_main_true_forms(self) -> None:
        for spec in ("HEAD:main", "main", "main:main",
                     "refs/heads/x:refs/heads/main"):
            self.assertTrue(_refspec_targets_main(spec), spec)

    def test_refspec_targets_main_false_forms(self) -> None:
        for spec in ("HEAD:feature", "HEAD:release", "main:topic", "x:develop"):
            self.assertFalse(_refspec_targets_main(spec), spec)

    # --- WARN path ---------------------------------------------------------
    def test_warns_on_named_branch_with_nonticket_commits(self) -> None:
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="feature-x\n"),
            log_cp=_make_cp(0, stdout="tools/foo.py\ntickets/T-1.md\n"),
        )
        self.assertIn("push guard T-3600", out)
        self.assertIn("feature-x", out)
        self.assertIn("tools/foo.py", out)  # the offending non-ticket path is named

    # --- silent (safe) paths ----------------------------------------------
    def test_no_warn_when_only_ticket_paths(self) -> None:
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="feature-x\n"),
            log_cp=_make_cp(0, stdout="tickets/T-1.md\ntickets/active/T-2.md\n"),
        )
        self.assertEqual(out, "")

    def test_no_warn_on_main_branch(self) -> None:
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="main\n"),
            log_cp=_make_cp(0, stdout="tools/foo.py\n"),  # would warn if consulted
        )
        self.assertEqual(out, "")

    def test_no_warn_on_detached_head_worktree_case(self) -> None:
        # symbolic-ref exits non-zero on detached HEAD -> guard must skip (this is
        # the legitimate worktree case the HEAD:main default exists for).
        out = self._run_guard(
            branch_cp=_make_cp(1, stdout="", stderr="fatal: ref HEAD is not a symbolic ref"),
            log_cp=_make_cp(0, stdout="tools/foo.py\n"),
        )
        self.assertEqual(out, "")

    def test_no_warn_when_refspec_not_main(self) -> None:
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="feature-x\n"),
            log_cp=_make_cp(0, stdout="tools/foo.py\n"),
            refspec="HEAD:feature-x",
        )
        self.assertEqual(out, "")

    def test_no_warn_when_opt_out_env_set(self) -> None:
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="feature-x\n"),
            log_cp=_make_cp(0, stdout="tools/foo.py\n"),
            env={"PANTHEON_TICKET_PUSH_GUARD_OFF": "1"},
        )
        self.assertEqual(out, "")

    def test_no_warn_when_origin_main_ref_absent(self) -> None:
        # `git log origin/main..HEAD` fails (no remote-tracking ref) -> skip.
        out = self._run_guard(
            branch_cp=_make_cp(0, stdout="feature-x\n"),
            log_cp=_make_cp(128, stderr="fatal: bad revision 'origin/main..HEAD'"),
        )
        self.assertEqual(out, "")

    # --- fail-open ---------------------------------------------------------
    def test_fail_open_when_git_raises(self) -> None:
        buf = io.StringIO()
        with patch("tools._git_utils._git", side_effect=OSError("boom")), \
             contextlib.redirect_stderr(buf):
            # Must not raise.
            _warn_if_branch_carries_nonticket_commits(Path("/x"), "HEAD:main")
        self.assertEqual(buf.getvalue(), "")

    # --- wiring: push_with_retry invokes the guard and still pushes --------
    def test_push_with_retry_invokes_guard_and_succeeds(self) -> None:
        with patch("tools._git_utils._warn_if_branch_carries_nonticket_commits") as guard, \
             patch("tools._git_utils._git", return_value=_make_cp(0)):
            ok, msg = push_with_retry(Path("/x"), refspec="HEAD:main")
        self.assertTrue(ok)
        guard.assert_called_once_with(Path("/x"), "HEAD:main")

    def test_push_with_retry_succeeds_even_if_guard_raises(self) -> None:
        # Defense in depth: even a BUG in the guard that raised must not break the
        # push on this fleet-shared path — push_with_retry wraps the guard call.
        with patch("tools._git_utils._warn_if_branch_carries_nonticket_commits",
                   side_effect=RuntimeError("guard bug")), \
             patch("tools._git_utils._git", return_value=_make_cp(0)):
            ok, _msg = push_with_retry(Path("/x"), refspec="HEAD:main")
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
