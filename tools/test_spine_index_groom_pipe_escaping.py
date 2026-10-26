"""Pipe-escaping tests for spine_index_groom row emission (T-1929).

A literal `|` inside a generated value splits the markdown table cell, so the
row renders with more columns than its header and the value is truncated at the
first pipe. Found during T-1912 planning: 7 frontmatter values across 5
conforming bugs/ files carry pipes, breaking 3 rows in bugs/README.md and 2 in
knowledge/episodic/README.md — 5 malformed rows repo-wide.

These assert on HANDLER OUTPUT, not on any particular escaping helper, so they
hold regardless of whether escaping is implemented at a shared choke point or
inline per emit site.

    python3 -m unittest tools.test_spine_index_groom_pipe_escaping -v

Import is `tools.<mod>` and the canonical invocation is from the repo root —
per bugs/tests-fire-real-telegram-via-run-as-script-dual-module-identity.md.
"""

from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

import tools.spine_index_groom as sig
from tools._tree_walk import walk_paths

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cells(row: str) -> list[str]:
    """Split a rendered markdown row into its cells on UNESCAPED pipes only."""
    out, buf, i = [], "", 0
    while i < len(row):
        if row[i] == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if row[i] == "|":
            out.append(buf)
            buf = ""
            i += 1
            continue
        buf += row[i]
        i += 1
    out.append(buf)
    # Leading/trailing pipes produce empty edge fields; drop them.
    return [c for c in out[1:-1]]


class TestPipeEscaping(unittest.TestCase):
    """Synthetic-dir tests: a pipe in a value must not create a new cell."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    # -- bugs_handler: 3-column (file | symptom | domain) ------------------

    def _bug(self, name: str, symptom: str, domain: str = "systemd") -> None:
        (self.d / name).write_text(
            f'---\nsymptom: "{symptom}"\nfix: "f"\ndomain: {domain}\n---\n# T\n'
        )

    def test_pipe_in_symptom_does_not_split_cell(self) -> None:
        self._bug("a.md", "guard of the form pgrep -af cargo | grep foo is unsound")
        row = sig.bugs_handler(self.d)[0]
        self.assertEqual(len(_cells(row)), 3, f"row split into extra cells: {row}")
        self.assertIn("\\|", row, "literal pipe was not escaped")

    def test_pipe_in_domain_does_not_split_cell(self) -> None:
        self._bug("a.md", "plain symptom", domain="bash | pipefail")
        row = sig.bugs_handler(self.d)[0]
        self.assertEqual(len(_cells(row)), 3, f"row split into extra cells: {row}")

    def test_multiple_pipes_in_one_value(self) -> None:
        self._bug("a.md", "a | b | c | d")
        row = sig.bugs_handler(self.d)[0]
        self.assertEqual(len(_cells(row)), 3, f"row split into extra cells: {row}")

    def test_value_without_pipe_is_not_altered(self) -> None:
        """Guard against over-escaping: no pipe in, no backslash out."""
        self._bug("a.md", "a perfectly ordinary symptom")
        row = sig.bugs_handler(self.d)[0]
        self.assertNotIn("\\", row)
        self.assertIn("a perfectly ordinary symptom", row)

    def test_escaped_value_content_survives_intact(self) -> None:
        """The pipe must remain visible as text, not be dropped."""
        self._bug("a.md", "left | right")
        row = sig.bugs_handler(self.d)[0]
        self.assertEqual(_cells(row)[1].strip(), "left | right")

    # -- double-escape hazard (T-1929, Orchestrator's explicit ask) ---------------

    def test_already_escaped_pipe_is_not_double_escaped(self) -> None:
        r"""A source value that already contains `\|` must keep ONE escape.

        Chosen behavior: escape only *unescaped* pipes. Naive replace() would
        turn `\|` into `\\|`, which markdown renders as a literal backslash
        followed by a LIVE cell break — i.e. still broken, and silently so.
        """
        self._bug("a.md", r"left \| right")
        row = sig.bugs_handler(self.d)[0]
        self.assertEqual(len(_cells(row)), 3, f"row split into extra cells: {row}")
        self.assertNotIn(r"\\|", row, "pipe was double-escaped")
        self.assertEqual(_cells(row)[1].strip(), "left | right")

    def test_escaping_is_idempotent(self) -> None:
        """Escape-twice == escape-once, so the choke point stays safe even if
        escaping were ever applied at more than one layer."""
        once = sig._md_cell("a | b")
        self.assertEqual(sig._md_cell(once), once)

    def test_backslash_not_before_pipe_is_untouched(self) -> None:
        r"""A lone backslash elsewhere in the value must not be disturbed."""
        self.assertEqual(sig._md_cell(r"C:\path and a | pipe"), r"C:\path and a \| pipe")

    # -- research_handler (knowledge/episodic): 2-column --------------------

    def test_pipe_in_episodic_description_does_not_split_cell(self) -> None:
        sub = self.d / "2026-05-07-thing"
        sub.mkdir()
        (sub / "README.md").write_text(
            "# Thing\n\n**Ticket:** T-0089 | **Compiled:** 2026-05-07 | **By:** x\n"
        )
        row = sig.research_handler(self.d)[0]
        self.assertEqual(len(_cells(row)), 2, f"row split into extra cells: {row}")


class TestLiveTree(unittest.TestCase):
    """Born-green guard: no AUTOGEN region anywhere may hold a malformed row.

    RED before the fix (5 malformed rows: 3 in bugs/README.md, 2 in
    knowledge/episodic/README.md), green after. Sibling precedent:
    test_tempfile_hygiene_lint.test_live_tools_tree_is_clean (T-1309).
    """

    def test_no_malformed_autogen_rows_repo_wide(self) -> None:
        malformed: list[str] = []
        # walk_paths, not rglob: tolerate entries vanishing mid-walk (T-4450).
        for md in walk_paths(REPO_ROOT, "*.md"):
            if ".git/" in str(md):
                continue
            try:
                lines = md.read_text().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            in_region, rows = False, []
            for ln in lines:
                if "AUTOGEN-START" in ln:
                    in_region = True
                    continue
                if "AUTOGEN-END" in ln:
                    in_region = False
                    continue
                if in_region and ln.strip().startswith("|"):
                    rows.append(ln.strip())
            # Skip markdown header-separator rows (|---|---|).
            rows = [r for r in rows if set(r) - set("|-: ")]
            if not rows:
                continue
            counts = [len(_cells(r)) for r in rows]
            modal = Counter(counts).most_common(1)[0][0]
            for r, c in zip(rows, counts):
                if c != modal:
                    rel = md.relative_to(REPO_ROOT)
                    malformed.append(f"{rel}: expected {modal} cells, got {c} — {r[:70]}")
        self.assertEqual(
            malformed,
            [],
            "malformed AUTOGEN rows (unescaped pipe splitting a cell):\n  "
            + "\n  ".join(malformed),
        )


if __name__ == "__main__":
    unittest.main()
