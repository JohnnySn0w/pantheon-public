"""Tests for stale_worktree_sweeper.py (T-1324b VPS disk-full prevention).

Safety-critical: prove the sweeper removes a worktree ONLY when every guard passes
(non-main, throwaway path, clean tree, merged HEAD) and NEVER an unmerged or dirty
one. git subprocess calls are mocked so no real worktrees are touched.

T-2616: also prove the age guard exempts only KNOWN-young worktrees, never widens
what an old/unmeasurable-age worktree is subject to (guards 3+4 still decide it).
"""
from __future__ import annotations

import io
import subprocess
import time
import unittest
from contextlib import redirect_stderr
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

    def _classify(self, wt, *, status_out="", status_rc=0, is_ancestor_rc=0,
                  stat_rc=0, stat_out="0"):
        """Drive classify with mocked git status + merge-base + stat(birth) results.

        Default stat_out="0" means "birth unmeasurable" (T-2616 fallback direction)
        so pre-existing tests that don't care about age keep their original guard
        3/4-only behavior.
        """
        def fake_run(user, args, **kw):
            if "status" in args:
                return _cp(status_rc, status_out)
            if "merge-base" in args:
                return _cp(is_ancestor_rc)
            if args[0] == "stat":
                return _cp(stat_rc, stat_out)
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

    # --- T-2616 age guard ---------------------------------------------------

    def test_young_worktree_exempt_even_when_clean_and_merged(self):
        birth = int(time.time() - 5 * 60)  # 5 minutes old, under default 60m
        ok, reason = self._classify(self._wt(), stat_out=str(birth))
        self.assertFalse(ok)
        self.assertIn("too young", reason)

    def test_old_worktree_still_removed(self):
        # Disk-safety must survive the age guard: an OLD clean+merged worktree
        # is removed exactly as before, once past the threshold.
        birth = int(time.time() - 120 * 60)  # 2 hours old, past default 60m
        ok, reason = self._classify(self._wt(), stat_out=str(birth))
        self.assertTrue(ok)
        self.assertIn("merged", reason)

    def test_unmeasurable_age_still_removed_and_warns(self):
        # Unknown age must fall toward the pre-T-2616 safe behavior (still
        # removed if clean+merged), never toward a new silent exemption — but
        # it must be LOUD, so a systemic stat failure doesn't quietly turn the
        # guard into a no-op that reaps fresh worktrees again.
        buf = io.StringIO()
        with redirect_stderr(buf):
            ok, reason = self._classify(self._wt(), stat_rc=1, stat_out="")
        self.assertTrue(ok)
        self.assertIn("merged", reason)
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("unmeasurable", buf.getvalue())

    def test_env_derived_threshold_respected(self):
        # MIN_AGE_MIN is env-derived (PANTHEON_WORKTREE_MIN_AGE_MIN); prove
        # classify() consults the module constant, not a hardcoded default —
        # patched here rather than importlib.reload to avoid bleeding module
        # state into other tests sharing this process (`pytest tools/ -q`).
        birth = int(time.time() - 5 * 60)  # 5 minutes old
        with mock.patch.object(sw, "MIN_AGE_MIN", 2.0):
            ok, reason = self._classify(self._wt(), stat_out=str(birth))
        self.assertTrue(ok)  # 5m now exceeds the lowered 2m threshold
        self.assertIn("merged", reason)


class WorktreeAgeMinutes(unittest.TestCase):
    """T-2616: _worktree_age_minutes reads birth time of `<path>/.git`."""

    def test_computes_minutes_from_birth_epoch(self):
        birth = int(time.time() - 600)  # 10 minutes ago
        with mock.patch.object(sw, "_run", return_value=_cp(0, str(birth))) as run:
            age = sw._worktree_age_minutes("claude", "/tmp/echo-T-1312")
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 10.0, delta=0.2)
        run.assert_called_once_with(
            "claude", ["stat", "-c", "%W", "/tmp/echo-T-1312/.git"]
        )

    def test_stat_failure_returns_none(self):
        with mock.patch.object(sw, "_run", return_value=_cp(1, "")):
            self.assertIsNone(sw._worktree_age_minutes("claude", "/tmp/x"))

    def test_zero_birth_returns_none(self):
        # %W == 0 means the filesystem/kernel doesn't expose birth time.
        with mock.patch.object(sw, "_run", return_value=_cp(0, "0")):
            self.assertIsNone(sw._worktree_age_minutes("claude", "/tmp/x"))

    def test_unparsable_stdout_returns_none(self):
        with mock.patch.object(sw, "_run", return_value=_cp(0, "not-a-number")):
            self.assertIsNone(sw._worktree_age_minutes("claude", "/tmp/x"))


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


class HealStaleGcLog(unittest.TestCase):
    """T-1691: clear a gc-blocking stale gc.log only when fsck proves it stale."""

    def _run(self, *, gc_exists, fsck_rc, rm_rc=0, clean=True):
        calls = []
        def fake_run(user, args, **kw):
            calls.append(args)
            joined = " ".join(args)
            if "test" in args and "-f" in args:
                return mock.Mock(returncode=0 if gc_exists else 1, stdout="", stderr="")
            if "fsck" in joined:
                return mock.Mock(returncode=fsck_rc, stdout="", stderr="corrupt" if fsck_rc else "")
            if args[0] == "rm":
                return mock.Mock(returncode=rm_rc, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(sw, "_run", side_effect=fake_run):
            msg = sw.heal_stale_gc_log("recovery", "/repo", clean=clean)
        return msg, calls

    def test_no_gc_log_is_noop(self):
        msg, _ = self._run(gc_exists=False, fsck_rc=0)
        self.assertIsNone(msg)

    def test_stale_log_clean_fsck_is_cleared(self):
        msg, calls = self._run(gc_exists=True, fsck_rc=0)
        self.assertIn("cleared", msg)
        self.assertTrue(any(a[0] == "rm" for a in calls), "should rm the gc.log")

    def test_failing_fsck_leaves_log(self):
        # Real corruption must stay loud — never clear the log.
        msg, calls = self._run(gc_exists=True, fsck_rc=1)
        self.assertIn("KEPT", msg)
        self.assertFalse(any(a[0] == "rm" for a in calls), "must NOT rm on failing fsck")

    def test_dry_run_reports_but_does_not_remove(self):
        msg, calls = self._run(gc_exists=True, fsck_rc=0, clean=False)
        self.assertIn("would-clear", msg)
        self.assertFalse(any(a[0] == "rm" for a in calls))


if __name__ == "__main__":
    unittest.main()
