"""Tests for tools.watch_premature_termination.

Run:
    python3 -m unittest tools.test_watch_premature_termination -v
"""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import watch_premature_termination as wpt


class WatchPrematureTerminationTests(unittest.TestCase):
    def test_thin_uncertain_negative_is_suspect(self) -> None:
        classification, tags = wpt.classify_excerpt("I could not determine whether the policy changed.")
        self.assertEqual(classification, "premature-termination-suspect")
        self.assertIn("uncertain-negative", tags)
        self.assertIn("no-instrument-evidence", tags)

    def test_multi_instrument_uncertain_negative_is_clearer(self) -> None:
        excerpt = "I could not determine a release after WebSearch and a direct GitHub API check across tags."
        classification, tags = wpt.classify_excerpt(excerpt)
        self.assertEqual(classification, "clear-negative")
        self.assertIn("multi-instrument-evidence", tags)

    def test_instrument_failure_without_fallback_is_suspect(self) -> None:
        excerpt = "Direct PDF extraction failed, so I could not confirm the table."
        classification, tags = wpt.classify_excerpt(excerpt)
        self.assertEqual(classification, "premature-termination-suspect")
        self.assertIn("instrument-failure", tags)

    def test_scan_reports_skips_readmes_and_limits_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / "2026-10-14"
            day.mkdir()
            (root / "README.md").write_text("I could not determine this should be skipped.\n", encoding="utf-8")
            (day / "one.md").write_text(
                textwrap.dedent(
                    """
                    # One

                    ## What Could Not Be Determined

                    I could not determine whether the vendor changed the quota page.
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            (day / "two.md").write_text(
                "I did not find a release after WebSearch and a GitHub tags check.\n",
                encoding="utf-8",
            )

            summary = wpt.scan_reports(root, include_clear=True, limit=1)

        self.assertEqual(summary.reports_scanned, 2)
        self.assertEqual(summary.candidate_count, 2)
        self.assertEqual(summary.suspect_count, 1)
        self.assertEqual(summary.clear_negative_count, 1)
        self.assertEqual(len(summary.findings), 1)
        payload = wpt.as_jsonable(summary)
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
