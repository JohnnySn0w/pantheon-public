#!/usr/bin/env python3
"""Tests for tools/memory_index.py (T-1130 / ORG-035)."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_memory_index` AND a standalone run from tools/.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import memory_index as mi  # noqa: E402


def _quiet_main(argv):
    """Run mi.main with stdout/stderr captured (keeps test output clean)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return mi.main(argv)


def _pin(d: Path, name: str, type_: str, desc: str = "d", pin_name: str | None = None) -> Path:
    p = d / name
    nm = pin_name or name[:-3]
    p.write_text(f"---\nname: {nm}\ndescription: {desc}\nmetadata:\n  type: {type_}\n---\nbody\n")
    return p


class SentinelRegionTests(unittest.TestCase):
    def test_none_when_no_region(self):
        self.assertIsNone(mi.read_sentinel_region("# Title\n\nno sentinels\n"))

    def test_splits_before_inside_after(self):
        text = (f"head\n{mi.SENTINEL_START}\ninside line\n{mi.SENTINEL_END}\ntail\n")
        before, inside, after = mi.read_sentinel_region(text)
        self.assertIn("head", before)
        self.assertEqual(inside, "inside line\n")
        self.assertIn("tail", after)


class RenderTests(unittest.TestCase):
    def test_empty_unfiled_emits_all_filed_marker(self):
        body = mi.render_region_body([])
        self.assertIn(mi.ALL_FILED_MARKER, body)
        self.assertNotIn("###", body)

    def test_groups_by_type_in_stable_order(self):
        pins = [
            {"filename": "reference_z.md", "type": "reference", "name": "z", "description": "dz"},
            {"filename": "user_a.md", "type": "user", "name": "a", "description": "da"},
            {"filename": "feedback_b.md", "type": "feedback", "name": "b", "description": ""},
        ]
        body = mi.render_region_body(pins)
        # user before feedback before reference (TYPE_ORDER).
        self.assertLess(body.index("### user"), body.index("### feedback"))
        self.assertLess(body.index("### feedback"), body.index("### reference"))
        # link + description rendering
        self.assertIn("- [a](user_a.md) — da", body)
        # missing description → no trailing em-dash
        self.assertIn("- [b](feedback_b.md)\n", body)


class FiledDetectionTests(unittest.TestCase):
    def test_only_curated_links_count(self):
        curated = "## Reference\n- [foo](reference_foo.md) — hook\n"
        self.assertEqual(mi.filed_filenames(curated), {"reference_foo.md"})


class BuildNewTextTests(unittest.TestCase):
    def _dir_with(self, pins_spec):
        d = Path(self._tmp.name)
        for name, type_ in pins_spec:
            _pin(d, name, type_)
        return d

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_unfiled_pin_appears_curated_preserved(self):
        d = self._dir_with([("feedback_new.md", "feedback")])
        current = "# Memory\n\n## Reference\n\nsome curated prose\n"
        out = mi.build_new_text(current, mi.scan_pins(d))
        # curated content preserved
        self.assertIn("some curated prose", out)
        self.assertIn(mi.SECTION_HEADING, out)
        # the unfiled pin is listed
        self.assertIn("(feedback_new.md)", out)

    def test_filed_pin_not_listed(self):
        d = self._dir_with([("reference_foo.md", "reference")])
        current = "# Memory\n\n## Reference\n- [foo](reference_foo.md) — hook\n"
        out = mi.build_new_text(current, mi.scan_pins(d))
        self.assertIn(mi.ALL_FILED_MARKER, out)
        # foo only appears once (the curated link), not duplicated in region
        self.assertEqual(out.count("reference_foo.md"), 1)

    def test_idempotent(self):
        d = self._dir_with([("feedback_new.md", "feedback")])
        current = "# Memory\n\ncurated\n"
        once = mi.build_new_text(current, mi.scan_pins(d))
        twice = mi.build_new_text(once, mi.scan_pins(d))
        self.assertEqual(once, twice)

    def test_filing_a_pin_removes_it_from_region(self):
        d = self._dir_with([("feedback_new.md", "feedback")])
        first = mi.build_new_text("# Memory\n\ncurated\n", mi.scan_pins(d))
        self.assertIn("(feedback_new.md)", first)
        # human files it into a curated section above the region
        filed = first.replace("curated\n", "curated\n- [new](feedback_new.md) — hook\n", 1)
        second = mi.build_new_text(filed, mi.scan_pins(d))
        # now the only occurrence is the curated one; region shows all-filed
        self.assertIn(mi.ALL_FILED_MARKER, second)
        self.assertEqual(second.count("feedback_new.md"), 1)

    def test_region_links_dont_count_as_filed(self):
        # A pin listed ONLY inside the region must stay "unfiled" across runs.
        d = self._dir_with([("feedback_new.md", "feedback")])
        first = mi.build_new_text("# Memory\n\ncurated\n", mi.scan_pins(d))
        second = mi.build_new_text(first, mi.scan_pins(d))
        self.assertIn("(feedback_new.md)", second)


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_dry_run_does_not_write(self):
        d = Path(self._tmp.name)
        _pin(d, "feedback_x.md", "feedback")
        idx = d / "MEMORY.md"
        idx.write_text("# Memory\n\ncurated\n")
        before = idx.read_text()
        rc = _quiet_main([str(d)])
        self.assertEqual(rc, 0)
        self.assertEqual(idx.read_text(), before)

    def test_apply_writes(self):
        d = Path(self._tmp.name)
        _pin(d, "feedback_x.md", "feedback")
        idx = d / "MEMORY.md"
        idx.write_text("# Memory\n\ncurated\n")
        rc = _quiet_main([str(d), "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn(mi.SECTION_HEADING, idx.read_text())

    def test_missing_index_returns_2(self):
        d = Path(self._tmp.name)
        self.assertEqual(_quiet_main([str(d)]), 2)


if __name__ == "__main__":
    unittest.main()
