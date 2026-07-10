#!/usr/bin/env python3
"""Tests for tools/memory_search.py (T-1129)."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_memory_search` AND a standalone run from tools/.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import memory_search as ms  # noqa: E402


class BuildCmdTests(unittest.TestCase):
    def test_default_cmd_shape(self):
        cmd = ms.build_rg_cmd("foo", "/m", names_only=False, ignore_case=True)
        self.assertEqual(cmd[0], "rg")
        self.assertIn("-i", cmd)
        self.assertIn("--no-heading", cmd)
        self.assertIn("*.md", cmd)
        # query + dir are the trailing args after the `--` separator.
        self.assertEqual(cmd[-3:], ["--", "foo", "/m"])

    def test_names_only_and_case_sensitive(self):
        cmd = ms.build_rg_cmd("Bar", "/m", names_only=True, ignore_case=False)
        self.assertIn("--files-with-matches", cmd)
        self.assertNotIn("-i", cmd)
        self.assertNotIn("--no-heading", cmd)


@unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
class RealRgTests(unittest.TestCase):
    def test_finds_match_and_no_match(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "feedback_a.md").write_text("---\nname: a\n---\nremember the telegram rule\n")
            (d / "reference_b.md").write_text("---\nname: b\n---\nsomething else\n")
            self.assertEqual(ms.main(["telegram", str(d)]), 0)        # has a match
            self.assertEqual(ms.main(["nonexistentxyz", str(d)]), 0)  # no match → still 0
            self.assertEqual(ms.main(["a", str(d), "--names-only"]), 0)

    def test_bad_dir_returns_2(self):
        self.assertEqual(ms.main(["x", "/no/such/dir"]), 2)


if __name__ == "__main__":
    unittest.main()
