#!/usr/bin/env python3
"""Tests for lint_report_day_index.py (T-1121)."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_lint_report_day_index` AND a standalone run from tools/.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import lint_report_day_index as lrd  # noqa: E402


def _silent_main(argv):
    """Run main() with stdout suppressed; return its exit code."""
    with contextlib.redirect_stdout(io.StringIO()):
        return lrd.main(argv)


class LinkExtractionTests(unittest.TestCase):
    def test_same_dir_md_links_captured(self):
        text = "| [topic-a](topic-a.md) | ok | x |\n| [b](b.md) | ok | y |"
        self.assertEqual(lrd.linked_md_targets(text), {"topic-a.md", "b.md"})

    def test_plain_text_fail_row_is_not_a_link(self):
        # A failed run documented as plain text must NOT count as a link.
        text = "| mcp-supply-chain | fail | non-zero exit (1); see log |"
        self.assertEqual(lrd.linked_md_targets(text), set())

    def test_cross_dir_and_url_links_ignored(self):
        text = ("[other](../2026-05-01/x.md) [u](https://e.com/y.md) "
                "[sub](sub/z.md) [ok](here.md)")
        self.assertEqual(lrd.linked_md_targets(text), {"here.md"})

    def test_non_md_links_ignored(self):
        text = "[log](run.log) [topic](topic.md)"
        self.assertEqual(lrd.linked_md_targets(text), {"topic.md"})


class CheckDayDirTests(unittest.TestCase):
    def _mkday(self, root: Path, name: str, readme: str, files: list[str]) -> Path:
        d = root / name
        d.mkdir()
        (d / "README.md").write_text(readme)
        for f in files:
            (d / f).write_text("# report\n")
        return d

    def test_consistent_dir_no_drift(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._mkday(Path(t), "2026-01-01",
                            "[a](a.md)\n[b](b.md)\n", ["a.md", "b.md"])
            self.assertEqual(lrd.check_day_dir(d), ([], []))

    def test_dead_link_detected(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._mkday(Path(t), "2026-01-02",
                            "[a](a.md)\n[gone](gone.md)\n", ["a.md"])
            self.assertEqual(lrd.check_day_dir(d), (["gone.md"], []))

    def test_unindexed_detected(self):
        with tempfile.TemporaryDirectory() as t:
            d = self._mkday(Path(t), "2026-01-03",
                            "[a](a.md)\n", ["a.md", "extra.md"])
            self.assertEqual(lrd.check_day_dir(d), ([], ["extra.md"]))

    def test_fail_row_plaintext_with_no_file_is_clean(self):
        # The real 05-25 shape: a fail row in plain text, no .md on disk → clean.
        with tempfile.TemporaryDirectory() as t:
            d = self._mkday(Path(t), "2026-01-04",
                            "[a](a.md)\n| mcp | fail | exit 1 |\n", ["a.md"])
            self.assertEqual(lrd.check_day_dir(d), ([], []))

    def test_missing_readme_surfaces_all_files_unindexed(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "2026-01-05"
            d.mkdir()
            (d / "a.md").write_text("x")
            self.assertEqual(lrd.check_day_dir(d), ([], ["a.md"]))


class LintAndMainTests(unittest.TestCase):
    def test_lint_only_reports_dirs_with_drift(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            clean = root / "2026-02-01"
            clean.mkdir()
            (clean / "README.md").write_text("[a](a.md)")
            (clean / "a.md").write_text("x")
            drift = root / "2026-02-02"
            drift.mkdir()
            (drift / "README.md").write_text("[a](a.md)")
            (drift / "a.md").write_text("x")
            (drift / "loose.md").write_text("x")
            findings = lrd.lint(root)
            self.assertEqual(set(findings), {"2026-02-02"})
            self.assertEqual(findings["2026-02-02"], ([], ["loose.md"]))

    def test_main_exit_1_on_drift_0_with_warn(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "2026-03-01"
            d.mkdir()
            (d / "README.md").write_text("[gone](gone.md)")
            (d / "real.md").write_text("x")
            self.assertEqual(_silent_main([str(t)]), 1)
            self.assertEqual(_silent_main([str(t), "--warn"]), 0)

    def test_main_exit_0_on_clean(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "2026-03-02"
            d.mkdir()
            (d / "README.md").write_text("[a](a.md)")
            (d / "a.md").write_text("x")
            self.assertEqual(_silent_main([str(t)]), 0)

    def test_single_day_dir_argument(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "reports"
            root.mkdir()
            good = root / "2026-04-01"
            good.mkdir()
            (good / "README.md").write_text("[a](a.md)")
            (good / "a.md").write_text("x")
            bad = root / "2026-04-02"
            bad.mkdir()
            (bad / "README.md").write_text("")
            (bad / "loose.md").write_text("x")
            # Linting only the good dir passes even though a sibling has drift.
            self.assertEqual(_silent_main([str(good)]), 0)
            self.assertEqual(_silent_main([str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
