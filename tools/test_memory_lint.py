#!/usr/bin/env python3
"""Tests for tools/memory_lint.py (T-1129 / ORG-035)."""

import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_memory_lint` AND a standalone run from tools/.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import memory_lint as ml  # noqa: E402


def _pin(d: Path, name: str, body: str) -> Path:
    p = d / name
    p.write_text(body)
    return p


class FrontmatterTests(unittest.TestCase):
    def test_top_level_type_variant(self):
        fm = ml.parse_frontmatter("name: X\ndescription: d\ntype: user\noriginSessionId: z")
        self.assertEqual(fm["_type"], "user")
        self.assertEqual(fm["name"], "X")

    def test_nested_metadata_type_variant(self):
        fm = ml.parse_frontmatter("name: x\ndescription: d\nmetadata:\n  node_type: memory\n  type: feedback")
        self.assertEqual(fm["_type"], "feedback")

    def test_split_frontmatter_none_when_absent(self):
        block, body = ml.split_frontmatter("no frontmatter here")
        self.assertIsNone(block)


class LintPinTests(unittest.TestCase):
    def _lint(self, body, name="feedback_x.md", stale=120, today=None):
        with tempfile.TemporaryDirectory() as t:
            p = _pin(Path(t), name, body)
            return ml.lint_pin(p, stale, today)

    def test_valid_pin_no_errors(self):
        errs, warns = self._lint("---\nname: feedback-x\ndescription: d\nmetadata:\n  type: feedback\n---\nbody\n")
        self.assertEqual(errs, [])

    def test_missing_name_and_desc_error(self):
        errs, _ = self._lint("---\ntype: feedback\n---\nbody\n")
        self.assertTrue(any("name" in e for e in errs))
        self.assertTrue(any("description" in e for e in errs))

    def test_invalid_type_error(self):
        errs, _ = self._lint("---\nname: x\ndescription: d\ntype: bogus\n---\nbody\n", name="feedback_x.md")
        self.assertTrue(any("bogus" in e for e in errs))

    def test_filename_prefix_mismatch_error(self):
        # file says reference_, frontmatter says feedback
        errs, _ = self._lint("---\nname: x\ndescription: d\ntype: feedback\n---\nbody\n", name="reference_x.md")
        self.assertTrue(any("disagrees" in e for e in errs))

    def test_empty_body_error(self):
        errs, _ = self._lint("---\nname: x\ndescription: d\ntype: user\n---\n   \n", name="user_x.md")
        self.assertTrue(any("body" in e for e in errs))

    def test_no_frontmatter_error(self):
        errs, _ = self._lint("just text, no frontmatter")
        self.assertTrue(any("frontmatter" in e for e in errs))

    def test_missing_provenance_is_warn_not_error(self):
        errs, warns = self._lint("---\nname: x\ndescription: d\ntype: feedback\n---\nbody\n")
        self.assertEqual(errs, [])
        self.assertTrue(any("provenance" in w for w in warns))

    def test_stale_verified_warns(self):
        # verified far in the past vs today_ord
        today = 2026 * 372 + 6 * 31 + 13
        body = "---\nname: x\ndescription: d\ntype: feedback\nverified: 2026-01-01\n---\nbody\n"
        _, warns = self._lint(body, today=today, stale=30)
        self.assertTrue(any("older than" in w for w in warns))

    def test_fresh_verified_no_stale_warn(self):
        today = 2026 * 372 + 6 * 31 + 13
        body = "---\nname: x\ndescription: d\ntype: feedback\nverified: 2026-06-10\n---\nbody\n"
        _, warns = self._lint(body, today=today, stale=30)
        self.assertFalse(any("older than" in w for w in warns))


class IndexDriftTests(unittest.TestCase):
    def test_unindexed_and_orphan(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _pin(d, "feedback_a.md", "x")
            _pin(d, "feedback_b.md", "x")
            (d / "MEMORY.md").write_text("- [A](feedback_a.md)\n- [gone](feedback_gone.md)\n")
            warns = ml.check_index(d)
            self.assertTrue(any("feedback_b.md" in w and "not indexed" in w for w in warns))
            self.assertTrue(any("feedback_gone.md" in w and "missing pin" in w for w in warns))

    def test_clean_index_no_warns(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _pin(d, "feedback_a.md", "x")
            (d / "MEMORY.md").write_text("- [A](feedback_a.md)\n")
            self.assertEqual(ml.check_index(d), [])


class MainTests(unittest.TestCase):
    def test_exit_0_clean_1_on_error(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            _pin(d, "feedback_ok.md", "---\nname: x\ndescription: d\ntype: feedback\nowner: orchestrator\nsource: s\ncreated: 2026-06-13\nupdated: 2026-06-13\nverified: 2026-06-13\nscope: local\n---\nbody\n")
            (d / "MEMORY.md").write_text("- [x](feedback_ok.md)\n")
            self.assertEqual(ml.main([str(d), "--today-ord", "100000000"]), 0)
            _pin(d, "feedback_bad.md", "no frontmatter")
            self.assertEqual(ml.main([str(d)]), 1)

    def test_strict_makes_warn_fail(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            # valid core but missing provenance → WARN; clean otherwise
            _pin(d, "user_x.md", "---\nname: x\ndescription: d\ntype: user\n---\nbody\n")
            (d / "MEMORY.md").write_text("- [x](user_x.md)\n")
            self.assertEqual(ml.main([str(d)]), 0)
            self.assertEqual(ml.main([str(d), "--strict"]), 1)


if __name__ == "__main__":
    unittest.main()
