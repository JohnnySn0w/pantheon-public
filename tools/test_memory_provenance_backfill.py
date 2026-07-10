#!/usr/bin/env python3
"""Tests for tools/memory_provenance_backfill.py (T-1131 / ORG-035)."""

import sys
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_memory_provenance_backfill` AND a standalone run.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import memory_provenance_backfill as bf  # noqa: E402

SHAPE_A = (
    "---\n"
    "name: feedback-x\n"
    "description: d\n"
    "metadata:\n"
    "  node_type: memory\n"
    "  type: feedback\n"
    "  originSessionId: f645f945-e0c1-40e4-b20b-aaeff9efa40f\n"
    "---\n"
    "body about msg_1271 here\n"
)

SHAPE_B = (
    "---\n"
    "name: feedback-y\n"
    "description: d\n"
    "type: feedback\n"
    "originSessionId: add312a9-bf5b-45a6-b773-2b7f667845ce\n"
    "---\n"
    "body\n"
)


class SplitTests(unittest.TestCase):
    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(bf.split_frontmatter_lines("no fm\n"))

    def test_splits_fm(self):
        pre, fm, rest = bf.split_frontmatter_lines(SHAPE_B)
        self.assertEqual(pre, ["---\n"])
        self.assertTrue(any("type: feedback" in ln for ln in fm))
        self.assertTrue(rest.startswith("---"))


class TopLevelKeysTests(unittest.TestCase):
    def test_excludes_nested(self):
        _, fm, _ = bf.split_frontmatter_lines(SHAPE_A)
        keys = bf.top_level_keys(fm)
        self.assertIn("name", keys)
        self.assertIn("metadata", keys)
        self.assertNotIn("type", keys)        # type is nested in shape A
        self.assertNotIn("node_type", keys)


class DeriveSourceFromFilenameTests(unittest.TestCase):
    def test_ticket_in_filename(self):
        self.assertEqual(bf.derive_source_from_filename("feedback_x_T-0444.md"), "T-0444")

    def test_msg_in_filename(self):
        self.assertEqual(bf.derive_source_from_filename("feedback_x_msg1271.md"), "msg1271")

    def test_none_when_not_in_filename(self):
        # body/frontmatter refs must NOT be used — only the filename.
        self.assertIsNone(bf.derive_source_from_filename("feedback_be_the_tech_guy.md"))


class BackfillTextTests(unittest.TestCase):
    def _bf(self, text, fn="feedback_x.md"):
        return bf.backfill_text(text, mtime_date="2026-06-02", owner="orchestrator",
                                scope="ho", filename=fn)

    def test_shape_a_inserts_before_metadata(self):
        # filename encodes msg1271 → source IS written
        out = self._bf(SHAPE_A, fn="feedback_x_msg1271.md")
        self.assertIsNotNone(out)
        # inserted top-level fields appear before the metadata: block
        self.assertLess(out.index("owner: orchestrator"), out.index("metadata:"))
        self.assertIn("scope: ho", out)
        self.assertIn("created: 2026-06-02", out)
        self.assertIn("updated: 2026-06-02", out)
        self.assertIn("source: msg1271", out)
        # nested metadata preserved, body untouched
        self.assertIn("  type: feedback", out)
        self.assertIn("body about msg_1271 here", out)

    def test_shape_b_no_filename_source_omits_source(self):
        out = self._bf(SHAPE_B, fn="feedback_y.md")
        self.assertIsNotNone(out)
        self.assertIn("owner: orchestrator", out)
        # no filename-encoded id → source LEFT for the audit, not written
        self.assertNotIn("source:", out)
        # top-level type: feedback preserved exactly once
        self.assertEqual(out.count("type: feedback"), 1)

    def test_idempotent_noop_when_present(self):
        out = self._bf(SHAPE_A)
        twice = self._bf(out)
        self.assertIsNone(twice)  # all derivable keys now present → no-op

    def test_partial_present_only_fills_missing(self):
        text = SHAPE_B.replace("type: feedback\n", "type: feedback\nowner: orchestrator\n")
        out = self._bf(text)
        self.assertIsNotNone(out)
        # owner already there → not duplicated
        self.assertEqual(out.count("owner: orchestrator"), 1)
        self.assertIn("scope: ho", out)

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(self._bf("just a body, no fm\n"))


if __name__ == "__main__":
    unittest.main()
