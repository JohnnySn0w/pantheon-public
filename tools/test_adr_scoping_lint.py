"""Unit tests for tools/adr_scoping_lint.py (T-2806, the operator TG 7590).

Run from repo root:
    python3 -m unittest tools.test_adr_scoping_lint -v
"""

from __future__ import annotations

import unittest

from tools import adr_scoping_lint


class TestCheckAdrFrontmatter(unittest.TestCase):
    """check_adr_frontmatter — pure function on one ADR file's full text."""

    def test_needs_build_without_scoping_ticket_is_a_violation(self) -> None:
        text = (
            "---\nid: ORG-900\nbuild_disposition: needs-build\n---\n\n"
            "# ORG-900\n\n**Status:** Proposed.\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-900-x.md", text)
        self.assertIsNotNone(msg)
        self.assertIn("adr/ORG-900-x.md", msg)
        self.assertIn("scoping_ticket", msg)

    def test_needs_build_with_scoping_ticket_passes(self) -> None:
        text = (
            "---\nid: ORG-901\nbuild_disposition: needs-build\n"
            "scoping_ticket: T-2814\n---\n\n# ORG-901\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-901-x.md", text)
        self.assertIsNone(msg)

    def test_no_build_needed_disposition_passes_without_scoping_ticket(self) -> None:
        text = "---\nid: ORG-902\nbuild_disposition: no-build-needed\n---\n\n# ORG-902\n"
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-902-x.md", text)
        self.assertIsNone(msg)

    def test_built_disposition_passes_without_scoping_ticket(self) -> None:
        text = "---\nid: ORG-903\nbuild_disposition: built\n---\n\n# ORG-903\n"
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-903-x.md", text)
        self.assertIsNone(msg)

    def test_absent_disposition_treated_as_needs_build_stricter_reading(self) -> None:
        """HO Q2 ruling 2026-08-10 (stricter reading): absent build_disposition
        is treated as needs-build for the CREATION lint -- a landing ADR must
        declare itself EITHER exempt OR carry a scoping_ticket."""
        text = "---\nid: ORG-904\n---\n\n# ORG-904\n"
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-904-x.md", text)
        self.assertIsNotNone(msg)

    def test_absent_disposition_with_scoping_ticket_passes(self) -> None:
        text = "---\nid: ORG-905\nscoping_ticket: T-3000\n---\n\n# ORG-905\n"
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-905-x.md", text)
        self.assertIsNone(msg)

    def test_no_frontmatter_at_all_treated_as_needs_build_no_scoping(self) -> None:
        text = "# ORG-906\n\nno frontmatter here.\n"
        msg = adr_scoping_lint.check_adr_frontmatter("adr/ORG-906-x.md", text)
        self.assertIsNotNone(msg)

    def test_real_org069_frontmatter_shape_passes(self) -> None:
        """D11 fixture provenance: keys copied from the real
        adr/ORG-069-passport-content-tool-authorization.md."""
        text = (
            "---\nid: ORG-069\nbuild_disposition: needs-build\n"
            "scoping_ticket: T-2814\n---\n\n# ORG-069\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter(
            "adr/ORG-069-passport-content-tool-authorization.md", text
        )
        self.assertIsNone(msg)


class TestScanDiffForNewAdrs(unittest.TestCase):
    """scan_diff_for_new_adrs — pure function on unified-diff text, mirrors
    tools.hermes_diff_scan.scan_diff_lines's shape."""

    def _new_file_diff(self, path: str, body_lines: list[str]) -> str:
        n = len(body_lines)
        hunk = "\n".join(f"+{line}" for line in body_lines)
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "index 0000000..abcdef1\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{n} @@\n"
            f"{hunk}\n"
        )

    def test_new_adr_needs_build_no_scoping_ticket_is_flagged(self) -> None:
        diff = self._new_file_diff(
            "adr/ORG-999-gap.md",
            ["---", "id: ORG-999", "build_disposition: needs-build", "---", "", "# ORG-999"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("adr/ORG-999-gap.md", violations[0])

    def test_new_adr_needs_build_with_scoping_ticket_is_clean(self) -> None:
        diff = self._new_file_diff(
            "adr/ORG-998-ok.md",
            [
                "---", "id: ORG-998", "build_disposition: needs-build",
                "scoping_ticket: T-3001", "---", "", "# ORG-998",
            ],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_new_adr_form_prefix_recognized(self) -> None:
        diff = self._new_file_diff(
            "adr/ADR-500-gap.md",
            ["---", "id: ADR-500", "build_disposition: needs-build", "---", "", "# ADR-500"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("adr/ADR-500-gap.md", violations[0])

    def test_modified_existing_adr_not_flagged_creation_lint_only(self) -> None:
        """T-2806 §4 scope: this lint only inspects NEWLY ADDED files -- a
        modified existing (non-conformant) ADR is the backstop's job
        (tools/ticket_grooming.py's ratified-unbuilt-ADR check), not this
        lint's."""
        diff = (
            "diff --git a/adr/ORG-070-x.md b/adr/ORG-070-x.md\n"
            "index abc123..def456 100644\n"
            "--- a/adr/ORG-070-x.md\n"
            "+++ b/adr/ORG-070-x.md\n"
            "@@ -1,3 +1,3 @@\n"
            " ---\n"
            "-id: ORG-070\n"
            "+id: ORG-070-renamed\n"
            " ---\n"
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_new_non_adr_file_ignored(self) -> None:
        diff = self._new_file_diff("tools/some_module.py", ["print('hi')"])
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_new_adr_no_build_needed_disposition_is_clean(self) -> None:
        diff = self._new_file_diff(
            "adr/ORG-997-policy.md",
            ["---", "id: ORG-997", "build_disposition: no-build-needed", "---", "", "# ORG-997"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_multiple_new_adrs_each_evaluated_independently(self) -> None:
        diff = self._new_file_diff(
            "adr/ORG-996-gap.md",
            ["---", "id: ORG-996", "build_disposition: needs-build", "---", "", "# ORG-996"],
        ) + self._new_file_diff(
            "adr/ORG-995-ok.md",
            [
                "---", "id: ORG-995", "build_disposition: needs-build",
                "scoping_ticket: T-3002", "---", "", "# ORG-995",
            ],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("adr/ORG-996-gap.md", violations[0])

    def test_empty_diff_no_violations(self) -> None:
        self.assertEqual(adr_scoping_lint.scan_diff_for_new_adrs(""), [])


class TestMain(unittest.TestCase):
    """CLI entry point exit codes (mirrors tools.hermes_diff_scan's contract)."""

    def test_main_returns_zero_on_clean_diff(self) -> None:
        import io
        from unittest.mock import patch

        diff = (
            "diff --git a/adr/ORG-994-ok.md b/adr/ORG-994-ok.md\n"
            "new file mode 100644\n"
            "index 0000000..abc\n"
            "--- /dev/null\n"
            "+++ b/adr/ORG-994-ok.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+---\n"
            "+build_disposition: no-build-needed\n"
            "+---\n"
        )
        with patch("sys.stdin", io.StringIO(diff)), patch("sys.argv", ["adr_scoping_lint.py"]):
            rc = adr_scoping_lint.main()
        self.assertEqual(rc, 0)

    def test_main_returns_one_on_violation(self) -> None:
        import io
        from unittest.mock import patch

        diff = (
            "diff --git a/adr/ORG-993-gap.md b/adr/ORG-993-gap.md\n"
            "new file mode 100644\n"
            "index 0000000..abc\n"
            "--- /dev/null\n"
            "+++ b/adr/ORG-993-gap.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+---\n"
            "+build_disposition: needs-build\n"
            "+---\n"
        )
        with patch("sys.stdin", io.StringIO(diff)), patch("sys.argv", ["adr_scoping_lint.py"]):
            rc = adr_scoping_lint.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
