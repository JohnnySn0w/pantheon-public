"""Unit tests for tools/hermes_diff_scan.py — the GitHub Action helper.

Run from repo root:
    python3 -m unittest tools.test_hermes_action -v

Covers:
  - scan_diff_lines: clean diff passes, unescaped trigger detected, escaped form
    passes, line-number accuracy, multiple violations, added-only (not removed)
  - format_annotation: GitHub Actions annotation format
  - Self-audit: hermes_diff_scan.py source must not contain the unescaped trigger

Fixture-string note: the unescaped trigger is constructed at runtime via
source-time string concatenation (b"HERME" + b"S.md" / "HERME" + "S.md") so
the contiguous 9-byte sequence never appears in this source file. This protects
`git log -p` of pantheon-config per the standing rule in `/home/claude/CLAUDE.md`
and the byte-sequence discipline convention in `tools/_git_utils.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.hermes_diff_scan import format_annotation, scan_diff_lines  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diff(hunks: list[str]) -> str:
    """Minimal unified-diff wrapper: wraps hunk content in enough header lines
    to be parseable by scan_diff_lines."""
    return "\n".join(hunks)


# ---------------------------------------------------------------------------
# scan_diff_lines tests
# ---------------------------------------------------------------------------


class TestScanDiffLines(unittest.TestCase):

    def test_clean_diff_passes(self) -> None:
        diff = (
            "diff --git a/file.md b/file.md\n"
            "--- a/file.md\n"
            "+++ b/file.md\n"
            "@@ -1,3 +1,4 @@\n"
            " context line\n"
            "+added clean line\n"
            " another context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(violations, [])

    def test_unescaped_trigger_detected(self) -> None:
        # Source-time concatenation; runtime yields the 9-byte trigger.
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/notes.md b/notes.md\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            f"+see {trigger} for context\n"
            " more context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path, "notes.md")

    def test_unescaped_trigger_line_number_accurate(self) -> None:
        # Hunk starts at line 5; trigger is on the second added line.
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/doc.txt b/doc.txt\n"
            "--- a/doc.txt\n"
            "+++ b/doc.txt\n"
            "@@ -5,3 +5,5 @@\n"
            " context line (line 5)\n"
            "+added line 6\n"
            f"+{trigger} on line 7\n"
            " context line (line 8)\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].lineno, 7)

    def test_escaped_form_passes(self) -> None:
        # Discipline-escaped form: literal backslash between S and dot.
        diff = (
            "diff --git a/rules.md b/rules.md\n"
            "--- a/rules.md\n"
            "+++ b/rules.md\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "+see HERMES\\.md for discipline details\n"
            " more context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(violations, [])

    def test_removed_lines_ignored(self) -> None:
        # A removed line containing the trigger must NOT be flagged — we only
        # care about newly-added bytes.
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/old.md b/old.md\n"
            "--- a/old.md\n"
            "+++ b/old.md\n"
            "@@ -1,3 +1,2 @@\n"
            " context\n"
            f"-removed line with {trigger}\n"
            " more context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(violations, [])

    def test_context_lines_ignored(self) -> None:
        # Context lines (no prefix or space prefix) must not be flagged.
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/ctx.md b/ctx.md\n"
            "--- a/ctx.md\n"
            "+++ b/ctx.md\n"
            "@@ -1,3 +1,4 @@\n"
            f" context: {trigger} in unchanged line\n"
            "+clean added line\n"
            " more context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(violations, [])

    def test_multiple_violations_all_reported(self) -> None:
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/multi.md b/multi.md\n"
            "--- a/multi.md\n"
            "+++ b/multi.md\n"
            "@@ -1,2 +1,4 @@\n"
            " context\n"
            f"+first {trigger} here\n"
            f"+second {trigger} here\n"
            " more context\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 2)

    def test_multiple_files_in_diff(self) -> None:
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/clean.md b/clean.md\n"
            "--- a/clean.md\n"
            "+++ b/clean.md\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            "+clean addition\n"
            " end\n"
            "diff --git a/dirty.md b/dirty.md\n"
            "--- a/dirty.md\n"
            "+++ b/dirty.md\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            f"+{trigger} inline\n"
            " end\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path, "dirty.md")

    def test_empty_diff_passes(self) -> None:
        violations = scan_diff_lines("")
        self.assertEqual(violations, [])

    def test_trigger_at_start_of_added_line(self) -> None:
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/start.md b/start.md\n"
            "--- a/start.md\n"
            "+++ b/start.md\n"
            "@@ -1,2 +1,3 @@\n"
            " context\n"
            f"+{trigger}: do not do this\n"
            " end\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].lineno, 2)

    def test_binary_diff_header_skipped(self) -> None:
        # Binary file diffs have no hunk lines; should not crash.
        diff = (
            "diff --git a/image.png b/image.png\n"
            "index abc123..def456 100644\n"
            "Binary files a/image.png and b/image.png differ\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(violations, [])

    def test_new_file_trigger_on_first_hunk_line(self) -> None:
        # New file mode; hunk starts at line 1.
        trigger = "HERME" + "S.md"
        diff = (
            "diff --git a/new.md b/new.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.md\n"
            "@@ -0,0 +1,2 @@\n"
            f"+{trigger}\n"
            "+second line\n"
        )
        violations = scan_diff_lines(diff)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].lineno, 1)
        self.assertEqual(violations[0].path, "new.md")


# ---------------------------------------------------------------------------
# format_annotation tests
# ---------------------------------------------------------------------------


class TestFormatAnnotation(unittest.TestCase):

    def test_annotation_format(self) -> None:
        from tools.hermes_diff_scan import Violation
        v = Violation(path="notes/foo.md", lineno=42)
        annotation = format_annotation(v)
        self.assertIn("::error", annotation)
        self.assertIn("notes/foo.md", annotation)
        self.assertIn("42", annotation)

    def test_annotation_contains_discipline_message(self) -> None:
        from tools.hermes_diff_scan import Violation
        v = Violation(path="ledger/2026.jsonl", lineno=7)
        annotation = format_annotation(v)
        # Must mention the violation in a way consistent with the local hook.
        self.assertIn("HERMES discipline violation", annotation)


# ---------------------------------------------------------------------------
# Self-audit: hermes_diff_scan.py must not contain the unescaped trigger
# ---------------------------------------------------------------------------


class TestDiffScanSourceCleanliness(unittest.TestCase):

    def test_hermes_diff_scan_source_clean(self) -> None:
        trigger = b"HERME" + b"S.md"
        path = REPO_ROOT / "tools" / "hermes_diff_scan.py"
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            "tools/hermes_diff_scan.py must not contain the unescaped trigger "
            "as contiguous bytes (byte-sequence discipline).",
        )

    def test_this_test_file_source_clean(self) -> None:
        trigger = b"HERME" + b"S.md"
        content = Path(__file__).read_bytes()
        self.assertNotIn(
            trigger,
            content,
            "tools/test_hermes_action.py must not contain the unescaped trigger "
            "as contiguous bytes (byte-sequence discipline).",
        )

    def test_workflow_yaml_source_clean(self) -> None:
        trigger = b"HERME" + b"S.md"
        path = REPO_ROOT / ".github" / "workflows" / "hermes-audit.yml"
        if not path.exists():
            self.skipTest("hermes-audit.yml not yet created")
        content = path.read_bytes()
        self.assertNotIn(
            trigger,
            content,
            ".github/workflows/hermes-audit.yml must not contain the unescaped "
            "trigger as contiguous bytes (byte-sequence discipline).",
        )


if __name__ == "__main__":
    unittest.main()
