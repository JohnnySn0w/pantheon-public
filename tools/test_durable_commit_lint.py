"""Tests for tools/durable_commit_lint.py (T-2595).

Run: python3 -m unittest tools.test_durable_commit_lint
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import durable_commit_lint as L  # noqa: E402


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


class DurableCommitLint(unittest.TestCase):
    # --- the enforcement: the live tree must be clean ---
    def test_real_tree_has_no_violations(self):
        """The T-2335 sweep verified the class is closed today; this asserts it
        stays closed. If it fails, a tool commits to tickets//ledger/ with a bare
        add+whole-index-commit and no lock — route it through commit_scoped or
        add a documented exception (see the tool's FIX message)."""
        self.assertEqual(L.lint(), [], "durable-commit lint found a live violation")

    # --- shown-to-fail: a bare committer MUST trip it ---
    def test_bare_durable_committer_is_a_violation(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "rogue_tool.py",
                   'from tools._git_utils import _git\n'
                   'def go():\n'
                   '    _git("add", "--", "tickets/T-1.md")\n'
                   '    _git("commit", "-m", "rogue")\n')
            self.assertIn("rogue_tool.py", L.lint(d))

    def test_ledger_bare_committer_also_tripped(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "rogue_ledger.py",
                   '_git("add", "--", "ledger/INDEX.md")\n_git("commit", "-m", "x")\n')
            self.assertIn("rogue_ledger.py", L.lint(d))

    # --- safe variants must NOT trip it ---
    def test_commit_scoped_delegate_is_safe(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "good_scoped.py",
                   'from tools._git_utils import commit_scoped\n'
                   'commit_scoped(["tickets/T-1.md"], "msg")\n')
            self.assertEqual(L.lint(d), [])

    def test_direct_commit_under_the_lock_is_safe(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "good_locked.py",
                   'from tools._git_utils import _git, git_mutation_lock\n'
                   'with git_mutation_lock():\n'
                   '    _git("add", "--", "tickets/T-1.md")\n'
                   '    _git("commit", "-m", "ok")\n')
            self.assertEqual(L.lint(d), [])

    # --- out of scope: read-only referencers + non-durable committers ---
    def test_read_only_referencer_is_out_of_scope(self):
        # references tickets/ + the word 'commit' but authors NO direct add/commit
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "readonly_lint.py",
                   '# reads commit hashes cited in tickets/, authors nothing\n'
                   '_git("rev-parse", "HEAD")\n_git("log", "tickets/")\n')
            self.assertEqual(L.lint(d), [])

    def test_non_durable_committer_is_out_of_scope(self):
        # direct commit but to a non-durable path (notes/), no tickets//ledger/
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "notes_tool.py",
                   '_git("add", "--", "notes/x.md")\n_git("commit", "-m", "note")\n')
            self.assertEqual(L.lint(d), [])

    # --- documented exceptions pass; test files + the lint itself are excluded ---
    def test_documented_exception_names_pass(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            # a bare committer, but named as a documented exception -> not flagged
            _write(d, "ticket.py",
                   '_git("add", "--", "tickets/T-1.md")\n_git("commit", "-m", "x")\n')
            self.assertEqual(L.lint(d), [])

    def test_test_files_are_excluded(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write(d, "test_rogue.py",
                   '_git("add", "--", "tickets/T-1.md")\n_git("commit", "-m", "fixture")\n')
            self.assertEqual(L.lint(d), [])


if __name__ == "__main__":
    unittest.main()
