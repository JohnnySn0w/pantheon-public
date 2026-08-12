"""Real-git integration tests for `pull_or_rebase_strict` (T-0846 held-gap fix).

Unlike `tools/test_git_utils.py` (which mocks subprocess.run), these tests drive
ACTUAL git against throwaway temp repos. The function's whole point is the
divergence-rebase reconcile path — a mocked git can't prove the local unpushed
commit is genuinely replayed onto the new origin tip, the conflict resolver is
genuinely invoked mid-rebase, or that an aborted rebase genuinely leaves a clean
tree. So we exercise real git here.

Scenarios:
  - ff happy path (local strictly behind)            -> (True, "")
  - divergence, non-overlapping changes              -> rebase, (True, "")
  - divergence, overlapping change, NO resolver       -> abort + clean, (False, ...)
  - divergence, overlapping change, resolver stages   -> (True, "")
  - ff refused but NOT ahead (dirty tree)             -> (False, "...not ahead...")

Runs as any account — no /tmp alloc-lock, no cross-user MAC (the ticket suite's
PermissionError on /tmp/pantheon_ticket_alloc.lock does not apply here).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import (
    PULL_NOT_AHEAD_PREFIX,
    PULL_REBASE_CONFLICT_PREFIX,
    _abort_stale_rebase,
    pull_or_rebase_strict,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _commit_file(cwd: Path, name: str, content: str, msg: str) -> None:
    (cwd / name).write_text(content)
    assert _git(cwd, "add", name).returncode == 0
    assert _git(cwd, "commit", "-m", msg).returncode == 0, msg


class PullOrRebaseStrictRealGit(unittest.TestCase):
    """Each test builds a bare origin + one local clone with the named topology."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.local = root / "local"
        self.other = root / "other"  # a second clone used to advance origin

        assert _git(root, "init", "--bare", "-b", "main", str(self.origin)).returncode == 0

        # Seed origin with an initial commit via the `other` clone.
        assert _git(root, "clone", str(self.origin), str(self.other)).returncode == 0
        self._config(self.other)
        _commit_file(self.other, "base.txt", "base\n", "seed")
        assert _git(self.other, "push", "origin", "main").returncode == 0

        # Local clone starts in sync with origin.
        assert _git(root, "clone", str(self.origin), str(self.local)).returncode == 0
        self._config(self.local)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, repo: Path) -> None:
        _git(repo, "config", "user.email", "t@t.test")
        _git(repo, "config", "user.name", "Test")

    def _advance_origin(self, name: str, content: str, msg: str) -> None:
        """Push a new origin/main commit from the `other` clone."""
        assert _git(self.other, "pull", "origin", "main").returncode == 0
        _commit_file(self.other, name, content, msg)
        assert _git(self.other, "push", "origin", "main").returncode == 0

    def _ahead_behind(self) -> tuple[int, int]:
        _git(self.local, "fetch", "origin", "main")
        r = _git(self.local, "rev-list", "--left-right", "--count",
                 "refs/remotes/origin/main...HEAD")
        behind, ahead = r.stdout.split()
        return int(ahead), int(behind)

    def _rebase_in_progress(self) -> bool:
        gd = self.local / ".git"
        return (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()

    # ----- scenarios -----

    def test_ff_happy_path_when_strictly_behind(self) -> None:
        self._advance_origin("a.txt", "a\n", "origin advances")
        ok, msg = pull_or_rebase_strict(cwd=self.local)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "")
        # Fast-forwarded: a.txt is now present, no rebase happened.
        self.assertTrue((self.local / "a.txt").exists())
        ahead, behind = self._ahead_behind()
        self.assertEqual((ahead, behind), (0, 0))

    def test_divergence_non_overlapping_rebases_clean(self) -> None:
        # Local: unpushed commit on local.txt. Origin: advanced on a.txt.
        _commit_file(self.local, "local.txt", "local\n", "local unpushed")
        self._advance_origin("a.txt", "a\n", "origin advances")

        ahead, behind = self._ahead_behind()
        self.assertEqual((ahead, behind), (1, 1))  # genuinely diverged

        ok, msg = pull_or_rebase_strict(cwd=self.local)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "")
        # Local commit replayed on top of origin's a.txt: both files present.
        self.assertTrue((self.local / "local.txt").exists())
        self.assertTrue((self.local / "a.txt").exists())
        # Now ahead 1 (the replayed commit), behind 0 — linear, ready to push.
        ahead, behind = self._ahead_behind()
        self.assertEqual((ahead, behind), (1, 0))
        self.assertFalse(self._rebase_in_progress())

    def test_divergence_conflict_no_resolver_aborts_clean(self) -> None:
        # Both sides edit shared.txt at the same spot -> genuine conflict.
        _commit_file(self.local, "shared.txt", "LOCAL\n", "local edits shared")
        self._advance_origin("shared.txt", "ORIGIN\n", "origin edits shared")

        ok, msg = pull_or_rebase_strict(cwd=self.local)
        self.assertFalse(ok)
        self.assertIn("conflict", msg.lower())
        # T-2333: ticket.py's hint-selector discriminates on this exact
        # prefix; pin it here so a wording drift in pull_or_rebase_strict
        # gets caught at the source, not as a mysteriously-wrong hint later.
        self.assertTrue(
            msg.startswith(PULL_REBASE_CONFLICT_PREFIX),
            f"rebase-conflict failure must start with the T-2333 prefix, got: {msg!r}",
        )
        # Aborted -> clean state, no rebase in progress, local commit intact.
        self.assertFalse(self._rebase_in_progress())
        self.assertEqual((self.local / "shared.txt").read_text(), "LOCAL\n")
        status = _git(self.local, "status", "--porcelain")
        self.assertEqual(status.stdout.strip(), "")

    def test_divergence_conflict_resolver_resolves(self) -> None:
        _commit_file(self.local, "shared.txt", "LOCAL\n", "local edits shared")
        self._advance_origin("shared.txt", "ORIGIN\n", "origin edits shared")

        def resolver(unmerged_paths, cwd):
            # Mechanical resolution: take a known value and stage it.
            self.assertEqual(set(unmerged_paths), {"shared.txt"})
            (Path(cwd) / "shared.txt").write_text("RESOLVED\n")
            return _git(Path(cwd), "add", "shared.txt").returncode == 0

        ok, msg = pull_or_rebase_strict(cwd=self.local, conflict_resolver=resolver)
        self.assertTrue(ok, msg)
        self.assertFalse(self._rebase_in_progress())
        self.assertEqual((self.local / "shared.txt").read_text(), "RESOLVED\n")

    def test_ff_refused_not_ahead_surfaces_error(self) -> None:
        # Origin advances base.txt; local has an uncommitted edit to that SAME
        # file, so the ff-only merge refuses (it would clobber local changes).
        # Local is NOT ahead -> nothing to rebase -> fail loud, no rebase.
        self._advance_origin("base.txt", "origin rewrote base\n", "origin edits base")
        (self.local / "base.txt").write_text("dirty uncommitted\n")

        ok, msg = pull_or_rebase_strict(cwd=self.local)
        self.assertFalse(ok)
        self.assertIn("not ahead", msg.lower())
        # T-2333: pin the exact prefix ticket.py's hint-selector matches on.
        self.assertTrue(
            msg.startswith(PULL_NOT_AHEAD_PREFIX),
            f"not-ahead failure must start with the T-2333 prefix, got: {msg!r}",
        )
        self.assertFalse(self._rebase_in_progress())

    # ----- T-1191: stale in-progress rebase self-heal -----

    def _wedge_stale_rebase(self) -> None:
        """Reproduce the 2026-06-15 wedge: a divergence whose rebase conflicts,
        started but NOT aborted (mimicking a process killed mid-rebase). Leaves
        `.git/rebase-merge/` behind so the next merge/pull would fail."""
        _commit_file(self.local, "shared.txt", "LOCAL\n", "local edits shared")
        self._advance_origin("shared.txt", "ORIGIN\n", "origin edits shared")
        assert _git(self.local, "fetch", "origin", "main").returncode == 0
        # This rebase conflicts; we deliberately leave it in progress (the
        # interrupted-mid-rebase state) instead of aborting.
        _git(self.local, "rebase", "refs/remotes/origin/main")
        assert self._rebase_in_progress(), "fixture should leave a stale rebase"

    def test_abort_stale_rebase_clears_wedge_then_noop(self) -> None:
        self._wedge_stale_rebase()
        # First call clears the stale rebase and reports it did so.
        self.assertTrue(_abort_stale_rebase(cwd=self.local))
        self.assertFalse(self._rebase_in_progress())
        # A clean clone -> second call is a no-op.
        self.assertFalse(_abort_stale_rebase(cwd=self.local))

    def test_abort_stale_rebase_in_linked_worktree(self) -> None:
        # Exercises the ABSOLUTE-path branch of `_abort_stale_rebase`: in a linked
        # worktree `git rev-parse --git-path rebase-merge` returns an absolute
        # per-worktree path (…/.git/worktrees/<n>/rebase-merge), not a cwd-relative
        # one. Locks in the relative-vs-absolute handling.
        wt = Path(self._tmp.name) / "wt"
        assert _git(self.local, "worktree", "add", "-b", "wtbranch", str(wt)).returncode == 0
        self._config(wt)
        # Wedge a stale rebase inside the worktree (conflict, left in progress).
        _commit_file(wt, "shared.txt", "WT\n", "wt edits shared")
        self._advance_origin("shared.txt", "ORIGIN\n", "origin edits shared")
        assert _git(wt, "fetch", "origin", "main").returncode == 0
        _git(wt, "rebase", "refs/remotes/origin/main")
        # Confirm the wedge exists from the worktree's own POV.
        gp = _git(wt, "rev-parse", "--git-path", "rebase-merge")
        rel = gp.stdout.strip()
        wedge_path = Path(rel) if os.path.isabs(rel) else (wt / rel)
        self.assertTrue(os.path.isabs(rel), "linked-worktree git-path should be absolute")
        self.assertTrue(wedge_path.exists(), "fixture should leave a stale rebase in the worktree")
        # The self-heal clears it via the absolute-path branch.
        self.assertTrue(_abort_stale_rebase(cwd=wt))
        self.assertFalse(wedge_path.exists())
        self.assertFalse(_abort_stale_rebase(cwd=wt))  # now a clean no-op

    def test_pull_or_rebase_strict_self_heals_stale_rebase(self) -> None:
        # With a stale rebase wedge present, pull_or_rebase_strict must NOT stay
        # wedged: it self-heals (aborts the leftover) before syncing. The
        # underlying conflict still resolves to a clean (False, conflict) — the
        # point is the clone is usable afterward, not wedged.
        self._wedge_stale_rebase()
        ok, msg = pull_or_rebase_strict(cwd=self.local)
        self.assertFalse(ok)
        self.assertIn("conflict", msg.lower())
        # The wedge is gone and the clone is in a clean, usable state.
        self.assertFalse(self._rebase_in_progress())
        status = _git(self.local, "status", "--porcelain")
        self.assertEqual(status.stdout.strip(), "")
        # Local unpushed commit survives (abort restored pre-rebase HEAD).
        self.assertEqual((self.local / "shared.txt").read_text(), "LOCAL\n")


if __name__ == "__main__":
    unittest.main()
