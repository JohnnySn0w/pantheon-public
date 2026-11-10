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

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Import the module under test — local import to pick up worktree version.
from tools._test_fixtures import (
    _RepoSnapshot,
    assert_real_repo_untouched,
    TicketTreeWatch,
    contained_path,
    copy_tools_import_closure,
    local_only_commits,
    snapshot_real_repo,
)


# ---------------------------------------------------------------------------
# Tests for synthetic-repo tools import closure
# ---------------------------------------------------------------------------


class TestCopyToolsImportClosure(unittest.TestCase):
    def test_new_import_and_its_transitive_import_are_copied_automatically(self) -> None:
        """A newly imported local module needs no hand-maintained copy list."""
        with tempfile.TemporaryDirectory(prefix="tfix_imports_") as tmp_str:
            base = Path(tmp_str)
            source_tools = base / "source" / "tools"
            destination_tools = base / "destination" / "tools"
            source_tools.mkdir(parents=True)
            (source_tools / "__init__.py").write_text("", encoding="utf-8")
            (source_tools / "ticket.py").write_text(
                "from tools import newly_added\n"
                "if True:\n"
                "    from tools import conditional_import\n"
                "try:\n"
                "    from tools import guarded_import\n"
                "except ImportError:\n"
                "    pass\n"
                "def lazy_import():\n"
                "    from tools import function_local_import\n"
                "class LazyImports:\n"
                "    from tools import class_local_import\n"
                "    def lazy_method(self):\n"
                "        from tools import method_local_import\n",
                encoding="utf-8",
            )
            (source_tools / "newly_added.py").write_text(
                "from tools.transitive_leaf import VALUE\n", encoding="utf-8"
            )
            (source_tools / "transitive_leaf.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            for module_name in (
                "conditional_import",
                "guarded_import",
                "function_local_import",
                "class_local_import",
                "method_local_import",
            ):
                (source_tools / f"{module_name}.py").write_text(
                    "VALUE = 1\n", encoding="utf-8"
                )

            copied = copy_tools_import_closure(
                destination_tools,
                source_tools=source_tools,
            )

            self.assertEqual(
                set(copied),
                {
                    Path("__init__.py"),
                    Path("ticket.py"),
                    Path("newly_added.py"),
                    Path("transitive_leaf.py"),
                    Path("conditional_import.py"),
                    Path("guarded_import.py"),
                    Path("class_local_import.py"),
                },
            )
            self.assertNotIn(Path("function_local_import.py"), copied)
            self.assertNotIn(Path("method_local_import.py"), copied)
            for relative_path in copied:
                self.assertEqual(
                    (destination_tools / relative_path).read_bytes(),
                    (source_tools / relative_path).read_bytes(),
                )

            imported = subprocess.run(
                [sys.executable, "-c", "import tools.ticket"],
                cwd=base / "destination",
                env={**os.environ, "PYTHONPATH": str(base / "destination")},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)


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
        # Guard must fire (T-5817: after a short settle window it never leaves).
        with self.assertRaises(AssertionError) as ctx:
            assert_real_repo_untouched(snap, settle_s=0.2)
        self.assertIn("new local-only commits", str(ctx.exception).lower())
        # T-4108: the message names the leaked commit's subject and paths, so a
        # leak caught by the wrong test under xdist still points at its source.
        self.assertIn("erroneous commit", str(ctx.exception))
        self.assertIn("bad.txt", str(ctx.exception))

    # --- T-5817: a concurrent actor's commit, pushed moments later -----------

    def test_concurrent_actor_commit_pushed_within_settle_passes(self) -> None:
        """Repro of orchestrator's 2026-11-01 soak failure: the live seat's own ticket.py
        committed to REPO_ROOT during the test and pushed it moments later. The
        guard re-reads and sees it reach origin/*, so it is not a leak."""
        import threading
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        _commit(repo, "tickets.txt", "tickets: clear stale auto_delivered (another actor)")
        pusher = threading.Timer(0.5, _push, args=(repo,))
        pusher.start()
        self.addCleanup(pusher.join)
        assert_real_repo_untouched(snap, settle_s=60)  # must not raise

    def test_leaked_commit_still_fails_after_settle(self) -> None:
        """A test's leaked commit is never pushed: the guard waits out the settle
        window and then fails, naming it."""
        import time
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        _commit(repo, "bad.txt", "erroneous commit")
        start = time.monotonic()
        with self.assertRaises(AssertionError) as ctx:
            assert_real_repo_untouched(snap, settle_s=1.0)
        self.assertGreaterEqual(time.monotonic() - start, 0.9)
        self.assertIn("bad.txt", str(ctx.exception))

    def test_clean_teardown_never_waits(self) -> None:
        """No new local-only SHA: no settle wait, even with a long window."""
        import time
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        start = time.monotonic()
        assert_real_repo_untouched(snap, settle_s=600)
        self.assertLess(time.monotonic() - start, 30)

    # --- T-4108: an unknown snapshot must not become an empty baseline -------

    def test_setup_snapshot_failure_does_not_false_positive(self) -> None:
        """Repro of the 2026-09-28 CI false positive. HEAD already carries a
        local-only commit (as a PR merge-ref checkout does), and the setUp
        snapshot's git call times out under IO contention. tearDown must NOT
        report that pre-existing commit as newly leaked."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        _commit(repo, "pr.txt", "Merge abc into def (PR merge ref)")  # local-only, pre-existing
        real_run = subprocess.run

        def timeout_git(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git log", timeout=1)

        with mock.patch("tools._test_fixtures.subprocess.run", side_effect=timeout_git):
            snap = snapshot_real_repo(repo_path=repo)
        self.assertIsNone(snap.local_only)
        self.assertIs(subprocess.run, real_run)
        with self.assertWarns(RuntimeWarning):
            assert_real_repo_untouched(snap)  # must not raise

    def test_teardown_read_failure_skips_not_raises(self) -> None:
        """A tearDown git failure is also UNKNOWN: skip with a warning, never
        diff against an empty set."""
        repo = _init_repo(self._base)
        _commit(repo, "a.txt", "init")
        _push(repo)
        snap = snapshot_real_repo(repo_path=repo)
        self.assertEqual(snap.local_only, frozenset())
        _commit(repo, "bad.txt", "erroneous commit")

        def timeout_git(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git log", timeout=1)

        with mock.patch("tools._test_fixtures.subprocess.run", side_effect=timeout_git):
            with self.assertWarns(RuntimeWarning):
                assert_real_repo_untouched(snap)  # unknown, so skipped

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


class TestContainedPath(unittest.TestCase):
    """T-4167: a fake `git mv` stub must never resolve a path outside its test root.

    pathlib discards the root for an absolute argv, so the pre-T-4167 stubs
    (`self.root / argv[2]`) would shutil.move a REAL file in the shared checkout.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.root.mkdir()
        self.outside = Path(tmp.name) / "real-checkout" / "tickets" / "active" / "T-9999.md"
        self.outside.parent.mkdir(parents=True)
        self.outside.write_text("live ticket\n")

    def test_relative_path_is_joined_under_the_root(self) -> None:
        self.assertEqual(contained_path(self.root, "tickets/active/T-0001.md"),
                         self.root / "tickets/active/T-0001.md")

    def test_absolute_path_inside_the_root_is_allowed(self) -> None:
        inside = str(self.root / "tickets" / "done" / "T-0001.md")
        self.assertEqual(contained_path(self.root, inside), Path(inside))

    def test_absolute_path_outside_the_root_is_refused_and_named(self) -> None:
        with self.assertRaises(AssertionError) as cm:
            contained_path(self.root, str(self.outside))
        self.assertIn(str(self.outside), str(cm.exception))
        self.assertTrue(self.outside.exists())

    def test_dotdot_escape_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            contained_path(self.root, "../real-checkout/tickets/active/T-9999.md")

    def test_symlink_out_of_the_root_is_refused(self) -> None:
        (self.root / "escape").symlink_to(self.outside.parent)
        with self.assertRaises(AssertionError):
            contained_path(self.root, "escape/T-9999.md")


class TestTicketTreeWatch(unittest.TestCase):
    """T-4167: the CI tripwire's drift check on a ticket tree."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tickets = Path(tmp.name) / "tickets"
        for state in ("inbox", "active", "done"):
            (self.tickets / state).mkdir(parents=True)
        (self.tickets / "active" / "T-4137.md").write_text("x\n")
        (self.tickets / "README.md").write_text("r\n")
        self.watch = TicketTreeWatch(self.tickets)

    def _bump(self, path: Path) -> None:
        # Make an mtime change visible even on a coarse-mtime filesystem.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def test_untouched_tree_reports_no_drift(self) -> None:
        token = self.watch.begin()
        (self.tickets / "active" / "T-4137.md").read_text()
        self.assertIsNone(self.watch.drift(token))

    def test_a_moved_ticket_is_reported_removed_and_added(self) -> None:
        token = self.watch.begin()
        (self.tickets / "active" / "T-4137.md").rename(self.tickets / "done" / "T-4137.md")
        self._bump(self.tickets / "active")
        self._bump(self.tickets / "done")
        self.assertEqual(self.watch.drift(token), (["active/T-4137.md"], ["done/T-4137.md"], []))

    def test_a_write_that_leaves_the_listing_unchanged_still_reports(self) -> None:
        token = self.watch.begin()
        tmp = self.tickets / "inbox" / ".scratch"
        tmp.write_text("x")
        tmp.unlink()
        self._bump(self.tickets / "inbox")
        self.assertEqual(self.watch.drift(token), ([], [], []))

    def test_a_new_top_level_entry_is_reported(self) -> None:
        token = self.watch.begin()
        (self.tickets / "stray.md").write_text("x")
        self._bump(self.tickets)
        self.assertEqual(self.watch.drift(token), ([], ["stray.md"], []))


class TestTicketTreeWatchGit(unittest.TestCase):
    """T-4167: a REAL `git pull` in the watched checkout is reported (the likely
    T-4165 mover), including when it brings no ticket changes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.origin = base / "origin.git"
        self.clone = base / "clone"
        self.other = base / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True,
                       capture_output=True)
        (self.other / "tickets" / "active").mkdir(parents=True)
        (self.other / "tickets" / "done").mkdir()
        (self.other / "tickets" / "active" / "T-4137.md").write_text("x\n")
        (self.other / "tickets" / "done" / ".keep").write_text("")
        (self.other / "tickets" / "active" / ".keep").write_text("")
        self._commit_push("seed")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True,
                       capture_output=True)
        self.watch = TicketTreeWatch(self.clone / "tickets", git_dir=self.clone / ".git")

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        *args], check=True, capture_output=True)

    def _commit_push(self, msg: str) -> None:
        self._git(self.other, "add", "-A")
        self._git(self.other, "commit", "-q", "--allow-empty", "-m", msg)
        self._git(self.other, "push", "-q", "origin", "HEAD:main")

    def _age(self) -> None:
        # Backdate the clone's markers + ticket dirs so a same-tick rewrite still
        # reads as a change on a coarse-mtime filesystem.
        for p in [*(self.clone / ".git").rglob("*"), *(self.clone / "tickets").rglob("*"),
                  self.clone / "tickets"]:
            os.utime(p, ns=(0, 1_000_000_000), follow_symlinks=False)

    def test_pull_that_resolves_a_ticket_upstream_is_reported(self) -> None:
        self._git(self.other, "mv", "tickets/active/T-4137.md", "tickets/done/T-4137.md")
        self._commit_push("resolve T-4137")
        self._age()
        token = self.watch.begin()
        self._git(self.clone, "pull", "-q", "--ff-only")
        removed, added, git_changed = self.watch.drift(token)
        self.assertEqual((removed, added), (["active/T-4137.md"], ["done/T-4137.md"]))
        self.assertIn("FETCH_HEAD", git_changed)
        self.assertIn("refs/heads/main", git_changed)

    def test_pull_with_no_ticket_changes_is_still_reported(self) -> None:
        self._commit_push("unrelated")
        self._age()
        token = self.watch.begin()
        self._git(self.clone, "pull", "-q", "--ff-only")
        removed, added, git_changed = self.watch.drift(token)
        self.assertEqual((removed, added), ([], []))
        self.assertIn("FETCH_HEAD", git_changed)

    def test_read_only_git_reports_nothing(self) -> None:
        self._age()
        token = self.watch.begin()
        self._git(self.clone, "rev-parse", "HEAD")
        self._git(self.clone, "log", "-1", "--format=%H")
        self.assertIsNone(self.watch.drift(token))


if __name__ == "__main__":
    unittest.main()
