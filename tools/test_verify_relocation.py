"""T-4745: tools/verify_relocation.py finds every dropped line, and only those."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import verify_relocation as vr


class VerifyRelocation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        (self.root / "adr").mkdir()
        (self.root / "notes").mkdir()
        self.adr = "adr/ORG-999-demo.md"
        (self.root / self.adr).write_text("# Demo\n\nDecision kept.\n\nBuild detail A.\nReview log B.\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8")

    def test_lossless_relocation_passes(self):
        self.write(self.adr, "# Demo\n\nDecision kept.\n\nSee notes.\n")
        self.write("notes/relocated.md", "Build detail A.\n")
        self.write("notes/history.md", "Review log B.\n")
        missing, total = vr.missing_lines(self.root, self.adr, "HEAD", ["notes/relocated.md", "notes/history.md"])
        self.assertEqual(missing, [])
        self.assertEqual(total, 4)

    def test_a_dropped_line_is_reported_with_its_old_number(self):
        self.write(self.adr, "# Demo\n\nDecision kept.\n")
        self.write("notes/relocated.md", "Build detail A.\n")
        missing, _ = vr.missing_lines(self.root, self.adr, "HEAD", ["notes/relocated.md"])
        self.assertEqual(missing, [(6, "Review log B.")])

    def test_a_short_line_is_not_satisfied_by_a_substring_or_a_shared_copy(self):
        # Review r1: "---" and "B." must not be matched inside longer lines, and a
        # line that occurred twice needs two surviving copies.
        self.write(self.adr, "# Demo\n---\nDecision kept.\n---\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "two rules")
        self.write(self.adr, "# Demo\n---\nDecision kept.\n")
        self.write("notes/relocated.md", "Text with --- inside it.\n")
        missing, _ = vr.missing_lines(self.root, self.adr, "HEAD", ["notes/relocated.md"])
        self.assertEqual(missing, [(4, "---")])

    def test_cli_exit_codes(self):
        self.write(self.adr, "# Demo\n\nDecision kept.\n")
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            self.assertEqual(vr.main([self.adr, "HEAD"]), 1)
            self.write("notes/all.md", "Build detail A.\nReview log B.\n")
            self.assertEqual(vr.main([self.adr, "HEAD", "notes/all.md"]), 0)
            self.assertEqual(vr.main([self.adr]), 2)
            self.assertEqual(vr.main([self.adr, "no-such-ref"]), 2)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
