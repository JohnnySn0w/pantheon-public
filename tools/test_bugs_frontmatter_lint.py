"""Tests for bugs_frontmatter_lint, plus the live-tree regression guard (T-1912).

    python3 -m unittest tools.test_bugs_frontmatter_lint -v

Import is `tools.<mod>` (never a sys.path.insert of tools/) and the canonical
invocation is from the repo root — per
bugs/tests-fire-real-telegram-via-run-as-script-dual-module-identity.md.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tools.bugs_frontmatter_lint as bfl

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFORMING = (
    '---\n'
    'symptom: "thing breaks in a visible way"\n'
    'fix: "do the surgical thing"\n'
    'domain: systemd, bun\n'
    '---\n'
    '# Title\n\nbody\n'
)


class TestScan(unittest.TestCase):
    """Detector unit tests against a synthetic bugs/ dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bugs = Path(self._tmp.name)

    def _write(self, name: str, text: str) -> None:
        (self.bugs / name).write_text(text)

    def test_conforming_file_is_clean(self) -> None:
        self._write("a.md", CONFORMING)
        self.assertEqual(bfl.scan(self.bugs), [])

    def test_no_frontmatter_flags_all_three_keys(self) -> None:
        self._write("a.md", "# Title\n\n**Symptom:** prose style, no frontmatter\n")
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["symptom", "fix", "domain"])])

    def test_missing_symptom_flagged(self) -> None:
        self._write("a.md", '---\nfix: "f"\ndomain: d\n---\n# T\n')
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["symptom"])])

    def test_missing_fix_flagged(self) -> None:
        """`fix` is contract-required even though the index renders only symptom+domain."""
        self._write("a.md", '---\nsymptom: "s"\ndomain: d\n---\n# T\n')
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["fix"])])

    def test_missing_domain_flagged(self) -> None:
        self._write("a.md", '---\nsymptom: "s"\nfix: "f"\n---\n# T\n')
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["domain"])])

    def test_empty_value_flagged(self) -> None:
        self._write("a.md", '---\nsymptom: ""\nfix: "f"\ndomain: d\n---\n# T\n')
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["symptom"])])

    def test_whitespace_only_value_flagged(self) -> None:
        self._write("a.md", '---\nsymptom: "   "\nfix: "f"\ndomain: d\n---\n# T\n')
        self.assertEqual(bfl.scan(self.bugs), [("a.md", ["symptom"])])

    def test_readme_is_excluded(self) -> None:
        self._write("README.md", "# bugs/\n\nno frontmatter here, by design\n")
        self.assertEqual(bfl.scan(self.bugs), [])

    def test_findings_sorted_by_filename(self) -> None:
        self._write("b.md", "# no fm\n")
        self._write("a.md", "# no fm\n")
        self.assertEqual([n for n, _ in bfl.scan(self.bugs)], ["a.md", "b.md"])

    def test_exit_code_clean(self) -> None:
        self._write("a.md", CONFORMING)
        self.assertEqual(bfl.main(["--bugs-dir", str(self.bugs), "--quiet"]), 0)

    def test_exit_code_violation(self) -> None:
        self._write("a.md", "# no fm\n")
        self.assertEqual(bfl.main(["--bugs-dir", str(self.bugs), "--quiet"]), 1)

    def test_missing_dir_is_error_exit_2(self) -> None:
        self.assertEqual(
            bfl.main(["--bugs-dir", str(self.bugs / "nope"), "--quiet"]), 2
        )


class TestLiveTree(unittest.TestCase):
    """Born-green guard: the real bugs/ tree must satisfy the contract.

    This is the enforcement surface — `pytest tools/` in CI turns a
    non-conforming new bug file into a red test. Sibling precedents:
    test_tempfile_hygiene_lint.test_live_tools_tree_is_clean (T-1309) and
    test_auto_commit_contract_lint.test_real_repo_has_no_violations.
    """

    def test_live_bugs_tree_is_clean(self) -> None:
        findings = bfl.scan(REPO_ROOT / "bugs")
        self.assertEqual(
            findings,
            [],
            "bugs/ files missing the symptom/fix/domain frontmatter contract "
            "(they render as placeholder rows in bugs/README.md's Auto-index): "
            + ", ".join(f"{n} [{', '.join(k)}]" for n, k in findings),
        )

    def test_live_tree_parses_with_the_generator_parser(self) -> None:
        """Guards the import-don't-reimplement decision: the lint and the index
        generator must agree, so assert the lint's cleanliness implies the
        generator renders no placeholder rows."""
        from tools.spine_index_groom import bugs_handler

        rows = bugs_handler(REPO_ROOT / "bugs")
        placeholders = [r for r in rows if "| — |" in r]
        self.assertEqual(placeholders, [], "generator still renders placeholder rows")


if __name__ == "__main__":
    unittest.main()
