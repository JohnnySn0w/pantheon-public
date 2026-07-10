"""Unit tests for the real-repo guard in tools/_test_fixtures.py.

Covers:
  - local_only_commits: correct output in all cases
  - snapshot_real_repo / assert_real_repo_untouched: correct behavior against
    a controlled tmp repo (not the live REPO_ROOT)

Key scenarios (per T-1034):
  A. No-origin-refs (fresh repo, no fetch): large baseline set is fine; delta
     is still empty unless a new local commit appears.
  B. External advancement via git pull: guard stays silent (new commit is
     reachable from origin/* after the pull).
  C. Genuine local commit (no push): guard RAISES AssertionError.
  D. No-op between snapshot and assert: guard stays silent.
  E. Commit + push: guard stays silent (commit is now reachable from origin).
  F. Snapshot returns _RepoSnapshot, not a raw str (regression guard).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

# Import the module under test — local import to pick up worktree version.
from tools._test_fixtures import (
    _RepoSnapshot,
    assert_real_repo_untouched,
    local_only_commits,
    snapshot_real_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(base: Path) -> Path:
    """Create a bare + working-tree pair. Returns the working tree path."""
    bare = base / "origin.git"
    bare.mkdir()
    _git(["git", "init", "--bare"], bare)

    repo = base / "repo"
    repo.mkdir()
    _git(["git", "init", "-b", "main"], repo)
    _git(["git", "config", "user.email", "test@example"], repo)
    _git(["git", "config", "user.name", "Test"], repo)
    _git(["git", "remote", "add", "origin", str(bare)], repo)
    return repo


def _commit(repo: Path, filename: str = "f.txt", msg: str = "c") -> str:
    """Write a file, stage, and commit. Returns the new HEAD SHA."""
    (repo / filename).write_text(f"{msg}\n", encoding="utf-8")
    _git(["git", "add", filename], repo)
    _git(["git", "commit", "-m", msg], repo)
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _push(repo: Path) -> None:
    _git(["git", "push", "origin", "main"], repo)


def _fetch(repo: Path) -> None:
    _git(["git", "fetch", "origin"], repo)


def _pull(repo: Path) -> None:
    _git(["git", "pull", "--ff-only", "origin", "main"], repo)


def _git(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"git command {cmd!r} failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )


# ---------------------------------------------------------------------------
# Tests for local_only_commits
# ---------------------------------------------------------------------------


class TestLocalOnlyCommits(unittest.TestCase):
    """Tests for the local_only_commits() primitive."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="tfix_test_")
        self._base = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_empty_repo_no_commits(self) -> None:
        """A freshly initialised repo with no commits returns frozenset()."""
        repo = self._base / "empty_repo"
        repo.mkdir()
        _git(["git", "init", "-b", "main"], repo)
        result = local_only_commits(repo)
        self.assertIsInstance(result, frozenset)
        self.assertEqual(result, frozenset())

    def test_no_origin_refs_all_commits_are_local_only(self) -> None:
        """When no origin/* refs exist, all commits are returned as local-only.

        This is the no-origin-refs edge case specified in the brief: a fresh
        repo where no fetch has been run yet.  The guard is still correct
        because snapshot captures the full set as baseline; tearDown only fires
        on new SHAs.
        """
        repo = _init_repo(self._base)
        sha1 = _commit(repo, "a.txt", "first")
        sha2 = _commit(repo, "b.txt", "second")
        # No push / fetch — no origin/* refs exist.
        result = local_only_commits(repo)
        self.assertIn(sha1, result)
        self.assertIn(sha2, result)

    def test_after_push_local_only_is_empty(self) -> None:
        """After pushing a commit, it is reachable from origin/* → not local-only."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "first")
        _push(repo)
        result = local_only_commits(repo)
        self.assertEqual(result, frozenset())

    def test_local_commit_not_yet_pushed(self) -> None:
        """A commit that exists locally but hasn't been pushed appears in the set."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "first")
        _push(repo)
        local_sha = _commit(repo, "b.txt", "second")
        # b.txt is committed but NOT pushed.
        result = local_only_commits(repo)
        self.assertIn(local_sha, result)
        self.assertEqual(len(result), 1)

    def test_after_push_previously_local_only_disappears(self) -> None:
        """After pushing, the formerly local-only commit is no longer returned."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "first")
        _push(repo)
        local_sha = _commit(repo, "b.txt", "second")
        self.assertIn(local_sha, local_only_commits(repo))
        _push(repo)
        self.assertNotIn(local_sha, local_only_commits(repo))


# ---------------------------------------------------------------------------
# Tests for snapshot_real_repo / assert_real_repo_untouched
# ---------------------------------------------------------------------------


class TestSnapshotAndAssert(unittest.TestCase):
    """Tests for snapshot_real_repo() and assert_real_repo_untouched()."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="tfix_assert_")
        self._base = Path(self._tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # --- (F) snapshot type ---------------------------------------------------

    def test_snapshot_returns_repo_snapshot_type(self) -> None:
        """snapshot_real_repo() returns _RepoSnapshot, not a raw str (regression guard)."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        self.assertIsInstance(snap, _RepoSnapshot)
        self.assertIsInstance(snap.local_only, frozenset)

    # --- (D) no-op -----------------------------------------------------------

    def test_no_op_does_not_trip_guard(self) -> None:
        """snapshot → no-op → assert: guard stays silent."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        # no-op
        assert_real_repo_untouched(snap)  # must not raise

    # --- (C) genuine local commit trips guard --------------------------------

    def test_genuine_local_commit_trips_guard(self) -> None:
        """A local commit (no push) after snapshot raises AssertionError."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        # Simulate a script erroneously committing to the "real" repo.
        _commit(repo, "bad.txt", "erroneous commit")
        # Guard must fire.
        with self.assertRaises(AssertionError) as ctx:
            assert_real_repo_untouched(snap)
        self.assertIn("new local-only commits", str(ctx.exception).lower())

    # --- (E) commit + push does NOT trip guard --------------------------------

    def test_pushed_commit_does_not_trip_guard(self) -> None:
        """A commit that is pushed before assert is reachable from origin/* → no trip."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        _commit(repo, "b.txt", "pushed commit")
        _push(repo)
        assert_real_repo_untouched(snap)  # must not raise

    # --- (B) external advancement via git pull --------------------------------

    def test_external_advancement_does_not_trip_guard(self) -> None:
        """Origin advances + local git pull: guard stays silent.

        This simulates the real multi-writer scenario: Specialist pushes to origin
        while a test is running, and the local repo (REPO_ROOT) receives a
        git pull.  After the pull, the new commit is reachable from origin/*
        (we verified via fetch), so local_only_commits returns the same set
        as before → no AssertionError.

        The test uses TWO working trees sharing one bare origin so the
        "external push" is exercised for real, not simulated.
        """
        # Set up: two clones of the same bare origin.
        bare = self._base / "origin.git"
        bare.mkdir()
        _git(["git", "init", "--bare"], bare)

        repo_a = self._base / "repo_a"
        repo_a.mkdir()
        _git(["git", "init", "-b", "main"], repo_a)
        _git(["git", "config", "user.email", "a@example"], repo_a)
        _git(["git", "config", "user.name", "A"], repo_a)
        _git(["git", "remote", "add", "origin", str(bare)], repo_a)

        repo_b = self._base / "repo_b"
        repo_b.mkdir()
        _git(["git", "init", "-b", "main"], repo_b)
        _git(["git", "config", "user.email", "b@example"], repo_b)
        _git(["git", "config", "user.name", "B"], repo_b)
        _git(["git", "remote", "add", "origin", str(bare)], repo_b)

        # Initial commit from repo_a, push.
        _commit(repo_a, "init.txt", "initial")
        _push(repo_a)

        # repo_b pulls so it's in sync.
        _git(["git", "fetch", "origin"], repo_b)
        _git(["git", "reset", "--hard", "origin/main"], repo_b)

        # Take snapshot of repo_b (simulates setUp on REPO_ROOT).
        snap = snapshot_real_repo(repo_path=repo_b)

        # "External actor" (repo_a) commits and pushes to origin.
        _commit(repo_a, "external.txt", "external push")
        _push(repo_a)

        # REPO_ROOT equivalent (repo_b) pulls the external advancement.
        # This is the critical step: after fetch, new commit is in origin/*.
        _fetch(repo_b)
        _pull(repo_b)

        # Guard must NOT trip.
        assert_real_repo_untouched(snap)  # must not raise

    # --- (A) no-origin-refs edge case ----------------------------------------

    def test_no_origin_refs_large_baseline_no_false_trip(self) -> None:
        """No-origin-refs: large baseline set → no false positive on no-op.

        When the repo has never been fetched, local_only_commits returns ALL
        commits (large set).  The guard captures this as baseline.  A
        subsequent no-op tearDown sees the same set → no false positive.
        """
        repo = _init_repo(self._base)
        # Several commits, none pushed.
        for i in range(3):
            _commit(repo, f"f{i}.txt", f"commit-{i}")
        # No push/fetch — no origin/* refs.
        snap = snapshot_real_repo(repo_path=repo)
        # No-op.
        assert_real_repo_untouched(snap)  # must not raise

    def test_no_origin_refs_genuine_commit_still_trips(self) -> None:
        """No-origin-refs: a new local commit after snapshot STILL trips the guard.

        Confirms the guard is not broken by the large-baseline case.
        """
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "initial")
        # No push/fetch — no origin/* refs.
        snap = snapshot_real_repo(repo_path=repo)
        # New local commit (simulating the bug we're guarding against).
        _commit(repo, "bad.txt", "erroneous")
        with self.assertRaises(AssertionError):
            assert_real_repo_untouched(snap)

    # --- external-advancement without pull (HEAD doesn't move) ---------------

    def test_external_push_without_local_pull_no_trip(self) -> None:
        """Origin advances but local repo is not pulled: guard stays silent.

        This is actually the more common scenario on this VPS: the real
        REPO_ROOT is not auto-pulled between every test.  The test confirms
        that origin advancing (without a local pull) is also harmless.
        """
        bare = self._base / "origin.git"
        bare.mkdir()
        _git(["git", "init", "--bare"], bare)

        repo_local = self._base / "local"
        repo_local.mkdir()
        _git(["git", "init", "-b", "main"], repo_local)
        _git(["git", "config", "user.email", "l@example"], repo_local)
        _git(["git", "config", "user.name", "L"], repo_local)
        _git(["git", "remote", "add", "origin", str(bare)], repo_local)

        repo_ext = self._base / "external"
        repo_ext.mkdir()
        _git(["git", "init", "-b", "main"], repo_ext)
        _git(["git", "config", "user.email", "e@example"], repo_ext)
        _git(["git", "config", "user.name", "E"], repo_ext)
        _git(["git", "remote", "add", "origin", str(bare)], repo_ext)

        # Initial commit.
        _commit(repo_local, "init.txt", "initial")
        _push(repo_local)
        # Sync external.
        _git(["git", "fetch", "origin"], repo_ext)
        _git(["git", "reset", "--hard", "origin/main"], repo_ext)

        # Snapshot (local HEAD doesn't move after this).
        snap = snapshot_real_repo(repo_path=repo_local)

        # External actor pushes a new commit.
        _commit(repo_ext, "ext.txt", "external")
        _push(repo_ext)
        # local_repo does NOT pull — HEAD stays put.

        # Guard must NOT trip (local HEAD didn't change; no new local-only).
        assert_real_repo_untouched(snap)  # must not raise


if __name__ == "__main__":
    unittest.main()
