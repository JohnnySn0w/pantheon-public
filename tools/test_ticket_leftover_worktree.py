"""T-4674: ticket.py resolve flags a worktree still on the ticket's agent branch.

Real git (the lookup parses `git worktree list --porcelain`), no network, no push.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import ticket


def _g(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class LeftoverWorktree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _g("init", "-q", "-b", "main", cwd=self.repo)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _g("config", k, v, cwd=self.repo)
        (self.repo / "f").write_text("x\n")
        _g("add", ".", cwd=self.repo)
        _g("commit", "-q", "-m", "base", cwd=self.repo)

    def tearDown(self):
        self._tmp.cleanup()

    def _add(self, branch):
        path = self.root / branch.replace("/", "_")
        _g("worktree", "add", "-q", "-b", branch, str(path), cwd=self.repo)
        return str(path)

    def test_finds_the_ticket_branch_and_a_suffixed_variant(self):
        exact = self._add("agent/T-9")
        squash = self._add("agent/T-9-squash")
        found = ticket._leftover_worktrees("T-9", self.repo)
        self.assertEqual(sorted(found), sorted([(exact, "agent/T-9"), (squash, "agent/T-9-squash")]))

    def test_a_longer_ticket_id_with_the_same_prefix_does_not_match(self):
        self._add("agent/T-90")
        self.assertEqual(ticket._leftover_worktrees("T-9", self.repo), [])

    def test_no_worktree_means_no_warning(self):
        err = io.StringIO()
        with mock.patch.object(ticket, "REPO_ROOT", self.repo), contextlib.redirect_stderr(err):
            ticket._warn_leftover_worktree("T-9")
        self.assertEqual(err.getvalue(), "")

    def test_warning_names_the_path_and_the_cleanup_command(self):
        path = self._add("agent/T-9")
        err = io.StringIO()
        with mock.patch.object(ticket, "REPO_ROOT", self.repo), contextlib.redirect_stderr(err):
            ticket._warn_leftover_worktree("T-9")
        self.assertIn(path, err.getvalue())
        self.assertIn("git worktree remove", err.getvalue())

    def test_not_a_repo_is_silent(self):
        self.assertEqual(ticket._leftover_worktrees("T-9", self.root / "nope"), [])


if __name__ == "__main__":
    unittest.main()
