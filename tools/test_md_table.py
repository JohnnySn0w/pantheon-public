"""Unit tests for tools/_md_table.py — the shared markdown-cell escaper (T-1936).

This is the single-implementation home for `_md_cell`/`_md_row` (re-homed from
spine_index_groom under T-1936). The escaping LOGIC lives here, so this is the
primary coverage; per-emitter wiring is proved in test_md_table_emitters.py by
calling the real emitter functions (never re-implementing the row).

Fixture-string note: the HERMES trigger byte sequence is never written in
contiguous form in this file (byte-sequence discipline).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._md_table import _md_cell, _md_row


class TestMdCell(unittest.TestCase):
    def test_single_pipe_escaped(self) -> None:
        self.assertEqual(_md_cell("a | b"), r"a \| b")

    def test_multiple_pipes_all_escaped(self) -> None:
        self.assertEqual(_md_cell("a|b|c"), r"a\|b\|c")

    def test_no_pipe_is_unchanged_byte_for_byte(self) -> None:
        # The no-churn guarantee at the cell level: pipe-free input is str() verbatim.
        for s in ("", "plain", "T-0001", "a → b", "`code`", "[x](y.md)"):
            self.assertEqual(_md_cell(s), s)

    def test_idempotent_already_escaped_stays_single(self) -> None:
        once = _md_cell("a | b")
        self.assertEqual(_md_cell(once), once, "escaping twice must equal escaping once")
        self.assertEqual(_md_cell(r"pre \| escaped"), r"pre \| escaped")

    def test_backslash_before_pipe_is_respected(self) -> None:
        # A real path fragment ending in a backslash directly before a pipe: the
        # pipe IS unescaped (the backslash escapes nothing of ours), so it escapes.
        self.assertEqual(_md_cell(r"C:\path and a | pipe"), r"C:\path and a \| pipe")

    def test_non_str_is_coerced(self) -> None:
        self.assertEqual(_md_cell(12), "12")
        self.assertEqual(_md_cell(None), "None")


class TestMdRow(unittest.TestCase):
    def test_assembles_delimiters_and_escapes_cells(self) -> None:
        self.assertEqual(_md_row("a", "b", "c"), "| a | b | c |")
        self.assertEqual(_md_row("a | x", "b"), r"| a \| x | b |")

    def test_delimiter_pipes_are_not_escaped(self) -> None:
        # The structural pipes _md_row adds must stay bare; only cell content escapes.
        row = _md_row("x", "y")
        self.assertEqual(row.count("|"), 3)          # 3 structural delimiters
        self.assertNotIn(r"\|", row)                 # nothing escaped — no cell had a pipe

    def test_byte_identical_to_handwritten_literal_for_pipe_free(self) -> None:
        # This equivalence is what lets every emitter adopt _md_row with a provable
        # zero-diff on existing (pipe-free) content.
        a, b, c = "T-0001", "inbox", "a title"
        self.assertEqual(_md_row(a, b, c), f"| {a} | {b} | {c} |")

    def test_single_cell_and_empty_cell(self) -> None:
        self.assertEqual(_md_row("x"), "| x |")
        self.assertEqual(_md_row(""), "|  |")


if __name__ == "__main__":
    unittest.main()
