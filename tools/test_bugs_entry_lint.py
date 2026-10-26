"""Tests for tools/bugs_entry_lint.py (T-4562). Run: python3 -m unittest tools.test_bugs_entry_lint"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import bugs_entry_lint as bel

WITH = "# Some bug\n\n## Symptom\nx\n\n## Verification\n`grep -c foo bar`\n"
WITHOUT = "# Some bug\n\n## Symptom\nx\n\n## Fix\ny\n"


class BugsEntryLintTest(unittest.TestCase):
    def test_new_entry_without_verification_is_flagged(self):
        out = bel.findings({"bugs/new-thing.md": WITHOUT})
        self.assertEqual(len(out), 1)
        self.assertIn("bugs/new-thing.md", out[0])

    def test_new_entry_with_verification_passes(self):
        self.assertEqual(bel.findings({"bugs/new-thing.md": WITH}), [])

    def test_heading_must_be_a_level_two_heading_at_line_start(self):
        prose = WITHOUT + "\nsee the ## Verification field\n### Verification\n"
        self.assertEqual(len(bel.findings({"bugs/x.md": prose})), 1)

    def test_readme_and_non_entries_are_exempt(self):
        self.assertEqual(bel.findings({
            "bugs/README.md": WITHOUT,
            "bugs/sub/dir.md": WITHOUT,
            "notes/x.md": WITHOUT,
            "bugs/x.txt": WITHOUT,
        }), [])


class PreCommitHookIntegrationTest(unittest.TestCase):
    """The real .githooks/pre-commit against a throwaway repo (T-4562)."""

    HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "pre-commit"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.local"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.repo, check=True)
        (self.repo / "bugs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _stage(self, name, text):
        (self.repo / "bugs" / name).write_text(text)
        subprocess.run(["git", "add", f"bugs/{name}"], cwd=self.repo, check=True)

    def _hook(self):
        return subprocess.run([sys.executable, str(self.HOOK)], cwd=self.repo,
                              capture_output=True, text=True, check=False)

    def test_added_entry_without_verification_is_refused(self):
        self._stage("new-bug.md", WITHOUT)
        r = self._hook()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("bugs/new-bug.md", r.stderr)

    def test_added_entry_with_verification_passes(self):
        self._stage("new-bug.md", WITH)
        self.assertEqual(self._hook().returncode, 0)

    def test_modified_legacy_entry_is_not_checked(self):
        self._stage("old-bug.md", WITHOUT)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "legacy"], cwd=self.repo, check=True)
        self._stage("old-bug.md", WITHOUT + "\nmore\n")
        self.assertEqual(self._hook().returncode, 0)


if __name__ == "__main__":
    unittest.main()
