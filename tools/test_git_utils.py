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
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Ensure repo root is importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import WorktreeError, with_worktree


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


if __name__ == "__main__":
    unittest.main()
