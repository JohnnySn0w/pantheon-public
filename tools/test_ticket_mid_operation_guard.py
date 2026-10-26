"""T-4422: ticket.py refuses to write while a rebase / merge / cherry-pick / revert is
in progress in the checkout. Real git throughout: the guard's whole point is the
on-disk state git leaves behind (`.git/rebase-merge`, `MERGE_HEAD`, ...)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.ticket as ticket  # noqa: E402
from tools._git_utils import _mid_operation_kind  # noqa: E402


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True).stdout


def _repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    _run(["git", "config", "core.hooksPath", "/dev/null"], repo)
    (repo / "a.md").write_text("one\n")
    _run(["git", "add", "a.md"], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    return repo


def _git_dir(repo: Path) -> Path:
    return Path(_run(["git", "rev-parse", "--absolute-git-dir"], repo).strip())


def _head(repo: Path) -> str:
    return _run(["git", "rev-parse", "HEAD"], repo).strip()


class TestMidOperationKind(unittest.TestCase):
    def test_each_marker_is_named_and_clean_reads_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            self.assertIsNone(_mid_operation_kind(repo))
            gd = _git_dir(repo)
            for marker, name in [("rebase-merge", "rebase"), ("rebase-apply", "rebase (am)"),
                                 ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                                 ("REVERT_HEAD", "revert")]:
                path = gd / marker
                path.mkdir() if marker.startswith("rebase") else path.write_text("0" * 40 + "\n")
                try:
                    self.assertEqual(_mid_operation_kind(repo), name, marker)
                finally:
                    path.rmdir() if marker.startswith("rebase") else path.unlink()
            self.assertIsNone(_mid_operation_kind(repo))

    def test_linked_worktree_sees_its_own_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            wt = Path(tmp) / "wt"
            _run(["git", "worktree", "add", "-q", "--detach", str(wt)], repo)
            (_git_dir(wt) / "rebase-merge").mkdir()
            self.assertEqual(_mid_operation_kind(wt), "rebase")
            self.assertIsNone(_mid_operation_kind(repo), "the main checkout is not mid-rebase")

    def test_not_a_repo_reads_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_mid_operation_kind(Path(tmp)))


class TestTicketWritesRefuseMidOperation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = _repo(Path(self._tmp.name))
        self._env = mock.patch.dict(os.environ, {
            "PANTHEON_GIT_MUTATION_LOCKFILE": str(Path(self._tmp.name) / "lock"),
        })
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_real_rebase_blocks_the_lock_before_the_body_runs(self) -> None:
        # A genuine conflicted rebase, not a hand-made marker.
        _run(["git", "checkout", "-q", "-b", "side"], self.repo)
        (self.repo / "a.md").write_text("side\n")
        _run(["git", "commit", "-q", "-am", "side"], self.repo)
        _run(["git", "checkout", "-q", "main"], self.repo)
        (self.repo / "a.md").write_text("main\n")
        _run(["git", "commit", "-q", "-am", "main"], self.repo)
        r = subprocess.run(["git", "rebase", "side"], cwd=str(self.repo), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, "fixture: rebase must stop on the conflict")
        self.assertEqual(_mid_operation_kind(self.repo), "rebase")
        ran = []
        with mock.patch.object(ticket, "REPO_ROOT", self.repo):
            with self.assertRaises(ticket.TicketError) as cm:
                with ticket.git_mutation_lock(label="test"):
                    ran.append(True)
        self.assertEqual(ran, [], "no ticket write may start inside a rebase")
        self.assertIn("rebase is in progress", str(cm.exception))
        self.assertIn("Nothing was staged, moved or committed", str(cm.exception))
        self.assertTrue((_git_dir(self.repo) / "rebase-merge").exists(), "the rebase is left as found")

    def test_lock_is_released_after_a_refusal(self) -> None:
        (_git_dir(self.repo) / "MERGE_HEAD").write_text(_head(self.repo) + "\n")
        with mock.patch.object(ticket, "REPO_ROOT", self.repo):
            with self.assertRaises(ticket.TicketError):
                with ticket.git_mutation_lock(label="first"):
                    pass
            (_git_dir(self.repo) / "MERGE_HEAD").unlink()
            with ticket.git_mutation_lock(label="second", timeout_secs=2.0):
                pass  # would time out if the refusal had leaked the lock

    def test_stage_and_commit_refuses_with_nothing_staged_or_committed(self) -> None:
        (_git_dir(self.repo) / "CHERRY_PICK_HEAD").write_text(_head(self.repo) + "\n")
        (self.repo / "a.md").write_text("edited\n")
        head = _head(self.repo)
        with self.assertRaises(ticket.TicketError) as cm:
            ticket._stage_and_commit_scoped([self.repo / "a.md"], "msg", repo_root=self.repo)
        self.assertIn("cherry-pick is in progress", str(cm.exception))
        self.assertEqual(_head(self.repo), head, "no commit")
        self.assertEqual(_run(["git", "diff", "--cached", "--name-only"], self.repo), "", "nothing staged")

    def test_clean_checkout_still_commits(self) -> None:
        (self.repo / "a.md").write_text("edited\n")
        head = _head(self.repo)
        with mock.patch.object(ticket, "REPO_ROOT", self.repo):
            with ticket.git_mutation_lock(label="clean"):
                ticket._stage_and_commit_scoped([self.repo / "a.md"], "ok", repo_root=self.repo)
        self.assertNotEqual(_head(self.repo), head)


if __name__ == "__main__":
    unittest.main()
