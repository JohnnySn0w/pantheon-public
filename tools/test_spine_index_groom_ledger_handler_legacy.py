"""Legacy regression tests for the ledger-handler helpers now in tools/spine_index_groom.py.

Originally tests for tools/ledger_index_groom.py (removed as part of T-0141 rename).
Retargeted to tools/spine_index_groom module; all public aliases preserved.
Idempotency, append-correctness, and section-handling are the load-bearing
behaviors. Auto-commit/push paths are unit-test-skipped (covered by integration
in the pantheon-grooming + watch_runner tests). HERMES guard is exercised via
_git_utils' own tests.
"""

from __future__ import annotations

import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import spine_index_groom as lig


class TestParseIndexedIds(unittest.TestCase):
    def test_extracts_backticked_ids(self):
        text = textwrap.dedent(
            """
            # Ledger Index

            ## 2026

            | ID | Action | Agent | Justifies-via |
            |---|---|---|---|
            | `2026-04-28T00:40:20Z-001` | ratify_principles_v0 | human:operator | P3, P5 |
            | `2026-04-28T01:46:19Z-001` | close_backlog_b3_adr_numbering | human:operator | P5 |
            """
        )
        self.assertEqual(
            lig.parse_indexed_ids(text),
            {"2026-04-28T00:40:20Z-001", "2026-04-28T01:46:19Z-001"},
        )

    def test_empty_index_returns_empty_set(self):
        self.assertEqual(lig.parse_indexed_ids("# Ledger Index\n\n## 2026\n\n"), set())

    def test_ignores_non_table_backticks(self):
        text = "Random `code` reference; not a table row.\n"
        self.assertEqual(lig.parse_indexed_ids(text), set())


class TestBuildIndexRow(unittest.TestCase):
    def test_full_entry(self):
        entry = {
            "id": "2026-05-04T22:00:00Z-001",
            "ts": "2026-05-04T22:00:00Z",
            "agent": "recovery",
            "action": "land_t0016_ledger_index_groomer",
            "justifies_via": ["P1", "P3", "D2"],
        }
        row = lig.build_index_row(entry)
        self.assertEqual(
            row,
            "| `2026-05-04T22:00:00Z-001` | land_t0016_ledger_index_groomer | recovery | P1, P3, D2 |",
        )

    def test_empty_justifies_renders_dash(self):
        entry = {
            "id": "2026-05-04T22:00:00Z-002",
            "agent": "recovery",
            "action": "noop",
            "justifies_via": [],
        }
        self.assertIn("| — |", lig.build_index_row(entry))

    def test_missing_justifies_renders_dash(self):
        entry = {
            "id": "2026-05-04T22:00:00Z-003",
            "agent": "recovery",
            "action": "noop",
        }
        self.assertIn("| — |", lig.build_index_row(entry))


class TestAppendRowsToYear(unittest.TestCase):
    def setUp(self):
        self.base = textwrap.dedent(
            """\
            # Ledger Index

            ## 2026

            | ID | Action | Agent | Justifies-via |
            |---|---|---|---|
            | `2026-04-28T00:40:20Z-001` | ratify_principles_v0 | human:operator | P3, P5 |

            (Some trailing prose describing the section.)
            """
        )

    def test_no_new_rows_returns_unchanged(self):
        self.assertEqual(lig.append_rows_to_year(self.base, "2026", []), self.base)

    def test_inserts_after_last_table_row(self):
        new = lig.append_rows_to_year(
            self.base,
            "2026",
            ["| `2026-05-04T22:00:00Z-001` | x | recovery | P1 |"],
        )
        # The new row should appear after the existing row but before the trailing prose.
        idx_existing = new.index("ratify_principles_v0")
        idx_new = new.index("2026-05-04T22:00:00Z-001")
        idx_prose = new.index("trailing prose")
        self.assertLess(idx_existing, idx_new)
        self.assertLess(idx_new, idx_prose)

    def test_creates_new_year_section_if_missing(self):
        new = lig.append_rows_to_year(
            self.base,
            "2027",
            ["| `2027-01-01T00:00:00Z-001` | new_year | recovery | P1 |"],
        )
        self.assertIn("## 2027", new)
        self.assertIn("2027-01-01T00:00:00Z-001", new)


class TestRunIdempotency(unittest.TestCase):
    """Integration-ish: run() against tmp ledger + index, verify add-then-noop."""

    def setUp(self):
        self.tmp = Path(self.id().replace(".", "_") + "_tmp")
        # We mock REPO_ROOT-relative paths via patch in the test.

    def test_first_run_appends_then_subsequent_noops(self):
        # Use real filesystem under a tmp dir we control.
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger_dir = tmp / "ledger"
            ledger_dir.mkdir()
            jsonl = ledger_dir / "2026.jsonl"
            index = ledger_dir / "INDEX.md"

            entries = [
                {"id": "2026-01-01T00:00:00Z-001", "agent": "recovery", "action": "a", "justifies_via": ["P1"]},
                {"id": "2026-01-02T00:00:00Z-001", "agent": "recovery", "action": "b", "justifies_via": ["P2"]},
            ]
            jsonl.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

            index.write_text(textwrap.dedent("""\
                # Ledger Index

                ## 2026

                | ID | Action | Agent | Justifies-via |
                |---|---|---|---|
                """))

            with patch.object(lig, "LEDGER_DIR", ledger_dir), \
                 patch.object(lig, "INDEX_PATH", index), \
                 patch.object(lig, "commit_and_push") as mock_commit:
                rc1 = lig.run("2026", dry_run=False, push=False)
                self.assertEqual(rc1, 0)
                self.assertEqual(mock_commit.call_count, 1)

                # Second run on the now-indexed state must be noop — no commit attempted.
                rc2 = lig.run("2026", dry_run=False, push=False)
                self.assertEqual(rc2, 0)
                self.assertEqual(mock_commit.call_count, 1)  # unchanged

            # Verify both entries landed.
            after = index.read_text()
            self.assertIn("2026-01-01T00:00:00Z-001", after)
            self.assertIn("2026-01-02T00:00:00Z-001", after)

    def test_dry_run_does_not_write(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ledger_dir = tmp / "ledger"
            ledger_dir.mkdir()
            jsonl = ledger_dir / "2026.jsonl"
            index = ledger_dir / "INDEX.md"

            jsonl.write_text(json.dumps({
                "id": "2026-01-01T00:00:00Z-001", "agent": "recovery",
                "action": "a", "justifies_via": ["P1"],
            }) + "\n")
            original_index = "# Ledger Index\n\n## 2026\n\n| ID | Action | Agent | Justifies-via |\n|---|---|---|---|\n"
            index.write_text(original_index)

            with patch.object(lig, "LEDGER_DIR", ledger_dir), \
                 patch.object(lig, "INDEX_PATH", index), \
                 patch.object(lig, "commit_and_push") as mock_commit:
                rc = lig.run("2026", dry_run=True, push=False)
                self.assertEqual(rc, 0)
                mock_commit.assert_not_called()

            # File untouched.
            self.assertEqual(index.read_text(), original_index)


if __name__ == "__main__":
    unittest.main()
