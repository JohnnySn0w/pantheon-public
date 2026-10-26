"""T-5316: a non-push `ticket.py file` failure must not leave the regenerated
tickets/README.md staged or dirty.

_commit_and_push rebuilds the board table before it stages and commits. When the
commit itself then fails (here: a refusing pre-commit hook), the board carried a
row for a ticket that never landed, and the next ticket op's scope guard refused
on the stray README. Real temp git repo + local bare origin (shared fixture).
"""

from __future__ import annotations

import subprocess
import unittest

from tools import ticket
from tools.test_ticket_file_overwrite_race import (  # noqa: F401 - module fixtures
    _RepoCase,
    setUpModule,
    tearDownModule,
)


class FileCommitFailureRestoresReadme(_RepoCase):
    def _refuse_commits(self) -> None:
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

    def _status(self) -> str:
        return subprocess.run(
            ["git", "status", "--porcelain", "--", "tickets/README.md"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout

    def _staged(self) -> str:
        return subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout

    def test_failed_commit_leaves_readme_clean_and_nothing_staged(self) -> None:
        self._refuse_commits()
        with self.assertRaises(Exception):
            ticket.cmd_file(self.file_args())
        self.assertEqual(self._status(), "", "README left staged/dirty after a failed commit")
        self.assertEqual(self._staged(), "", "files left staged after a failed commit")
        self.assertEqual(list(self.inbox().glob("T-*.md")), [], "ghost ticket left in inbox/")

    def test_next_filing_succeeds_after_a_failed_commit(self) -> None:
        self._refuse_commits()
        with self.assertRaises(Exception):
            ticket.cmd_file(self.file_args())
        (self.root / ".git" / "hooks" / "pre-commit").unlink()
        # No hand cleanup: the failed attempt must leave nothing in the way.
        self.assertEqual(ticket.cmd_file(self.file_args(title="Second attempt")), 0)

    def test_scope_refusal_leaves_readme_clean_and_next_filing_succeeds(self) -> None:
        # A stray pre-staged path makes the scope guard refuse after the board
        # rebuild; the README must not be left dirty by that refusal.
        (self.root / "stray.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "stray.txt"], cwd=self.root, check=True)
        with self.assertRaises(Exception):
            ticket.cmd_file(self.file_args())
        self.assertEqual(self._status(), "", "README left dirty after a scope refusal")
        subprocess.run(["git", "reset", "-q", "--", "stray.txt"], cwd=self.root, check=True)
        self.assertEqual(ticket.cmd_file(self.file_args(title="After refusal")), 0)

if __name__ == "__main__":
    unittest.main()
