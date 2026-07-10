"""Tests for stale_worktree_sweeper.py (T-1324b VPS disk-full prevention).

Safety-critical: prove the sweeper removes a worktree ONLY when every guard passes
(non-main, throwaway path, clean tree, merged HEAD) and NEVER an unmerged or dirty
one. git subprocess calls are mocked so no real worktrees are touched.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.stale_worktree_sweeper as sw


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


class Classify(unittest.TestCase):
    REPO = "/home/claude/echo-prismatic"

    def _classify(self, wt, *, status_out="", status_rc=0, is_ancestor_rc=0):
        """Drive classify with mocked git status + merge-base results."""
        def fake_run(user, args, **kw):
            if "status" in args:
                return _cp(status_rc, status_out)
            if "merge-base" in args:
                return _cp(is_ancestor_rc)
            return _cp(0)
        with mock.patch.object(sw, "_run", side_effect=fake_run):
            return sw.classify("claude", self.REPO, wt)

    def _wt(self, path="/tmp/echo-T-1312", main=False, head="abc123"):
        return {"path": path, "head": head, "branch": "refs/heads/x", "main": main}

    def test_main_checkout_kept(self):
        ok, reason = self._classify(self._wt(main=True))
        self.assertFalse(ok)
        self.assertIn("main", reason)

    def test_non_throwaway_path_kept(self):
        ok, reason = self._classify(self._wt(path="/home/claude/echo-prismatic-wt"))
        self.assertFalse(ok)
        self.assertIn("throwaway", reason)

    def test_dirty_worktree_kept(self):
        ok, reason = self._classify(self._wt(), status_out=" M src/main.rs")
        self.assertFalse(ok)
        self.assertIn("dirty", reason)

    def test_unmerged_head_kept(self):
        # merge-base --is-ancestor returns nonzero → HEAD not in main → keep.
        ok, reason = self._classify(self._wt(), is_ancestor_rc=1)
        self.assertFalse(ok)
        self.assertIn("UNMERGED", reason)

    def test_merged_clean_throwaway_removable(self):
        ok, reason = self._classify(self._wt(), status_out="", is_ancestor_rc=0)
        self.assertTrue(ok)
        self.assertIn("merged", reason)

    def test_no_head_kept(self):
        wt = self._wt()
        wt["head"] = None
        ok, reason = self._classify(wt)
        self.assertFalse(ok)
        self.assertIn("no HEAD", reason)

    def test_status_failure_kept(self):
        ok, reason = self._classify(self._wt(), status_rc=128)
        self.assertFalse(ok)
        self.assertIn("status failed", reason)


class ListWorktrees(unittest.TestCase):
    PORCELAIN = (
        "worktree /home/claude/echo-prismatic\nHEAD aaa\nbranch refs/heads/main\n\n"
        "worktree /tmp/echo-T-1312\nHEAD bbb\nbranch refs/heads/T-1312\n\n"
    )

    def test_parses_and_marks_main(self):
        with mock.patch.object(sw, "_git", return_value=_cp(0, self.PORCELAIN)):
            trees = sw.list_worktrees("claude", "/home/claude/echo-prismatic")
        self.assertEqual(len(trees), 2)
        self.assertTrue(trees[0]["main"])
        self.assertFalse(trees[1]["main"])
        self.assertEqual(trees[1]["path"], "/tmp/echo-T-1312")
        self.assertEqual(trees[1]["head"], "bbb")

    def test_missing_repo_returns_empty(self):
        with mock.patch.object(sw, "_git", return_value=_cp(128, "")):
            self.assertEqual(sw.list_worktrees("claude", "/nope"), [])


class SweepDryRun(unittest.TestCase):
    def test_dry_run_never_calls_remove(self):
        wt_main = {"path": "/home/claude/echo-prismatic", "head": "a", "main": True}
        wt_stale = {"path": "/tmp/echo-T-1312", "head": "b", "main": False}
        with mock.patch.object(sw, "REPOS", [("claude", "/home/claude/echo-prismatic")]), \
             mock.patch.object(sw, "list_worktrees", return_value=[wt_main, wt_stale]), \
             mock.patch.object(sw, "classify", side_effect=lambda u, r, wt: (not wt["main"], "x")), \
             mock.patch.object(sw, "_du_gib", return_value=12.0), \
             mock.patch.object(sw, "_git") as git:
            n = sw.sweep(clean=False)
        self.assertEqual(n, 1)
        # No worktree-remove issued in dry-run.
        for call in git.call_args_list:
            self.assertNotIn("remove", call.args)


if __name__ == "__main__":
    unittest.main()
