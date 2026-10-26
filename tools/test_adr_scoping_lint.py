"""Unit tests for tools/adr_scoping_lint.py (T-2806, the operator TG 7590).

Run from repo root:
    python3 -m unittest tools.test_adr_scoping_lint -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import adr_scoping_lint


class TestCheckAdrFrontmatter(unittest.TestCase):
    """check_adr_frontmatter — pure function on one ADR file's full text."""

    def test_needs_build_without_scoping_ticket_is_a_violation(self) -> None:
        text = (
            "---\nid: ORG-900\nbuild_disposition: needs-build\n---\n\n"
            "# ORG-900\n\n**Status:** Proposed.\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-900-x.md", text)
        self.assertIsNotNone(msg)
        self.assertIn("architecture/adr/ORG-900-x.md", msg)
        self.assertIn("scoping_ticket", msg)

    def test_needs_build_with_scoping_ticket_passes(self) -> None:
        text = (
            "---\nid: ORG-901\nbuild_disposition: needs-build\n"
            "scoping_ticket: T-2814\n---\n\n# ORG-901\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-901-x.md", text)
        self.assertIsNone(msg)

    def test_no_build_needed_disposition_passes_without_scoping_ticket(self) -> None:
        text = "---\nid: ORG-902\nbuild_disposition: no-build-needed\n---\n\n# ORG-902\n"
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-902-x.md", text)
        self.assertIsNone(msg)

    def test_built_disposition_passes_without_scoping_ticket(self) -> None:
        text = "---\nid: ORG-903\nbuild_disposition: built\n---\n\n# ORG-903\n"
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-903-x.md", text)
        self.assertIsNone(msg)

    def test_absent_disposition_treated_as_needs_build_stricter_reading(self) -> None:
        """HO Q2 ruling 2026-08-10 (stricter reading): absent build_disposition
        is treated as needs-build for the CREATION lint -- a landing ADR must
        declare itself EITHER exempt OR carry a scoping_ticket."""
        text = "---\nid: ORG-904\n---\n\n# ORG-904\n"
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-904-x.md", text)
        self.assertIsNotNone(msg)

    def test_absent_disposition_with_scoping_ticket_passes(self) -> None:
        text = "---\nid: ORG-905\nscoping_ticket: T-3000\n---\n\n# ORG-905\n"
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-905-x.md", text)
        self.assertIsNone(msg)

    def test_no_frontmatter_at_all_treated_as_needs_build_no_scoping(self) -> None:
        text = "# ORG-906\n\nno frontmatter here.\n"
        msg = adr_scoping_lint.check_adr_frontmatter("architecture/adr/ORG-906-x.md", text)
        self.assertIsNotNone(msg)

    def test_real_org069_frontmatter_shape_passes(self) -> None:
        """D11 fixture provenance: keys copied from the real
        architecture/adr/ORG-069-passport-content-tool-authorization.md."""
        text = (
            "---\nid: ORG-069\nbuild_disposition: needs-build\n"
            "scoping_ticket: T-2814\n---\n\n# ORG-069\n"
        )
        msg = adr_scoping_lint.check_adr_frontmatter(
            "architecture/adr/ORG-069-passport-content-tool-authorization.md", text
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
            "architecture/adr/ORG-999-gap.md",
            ["---", "id: ORG-999", "build_disposition: needs-build", "---", "", "# ORG-999"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/adr/ORG-999-gap.md", violations[0])

    def test_new_adr_needs_build_with_scoping_ticket_is_clean(self) -> None:
        diff = self._new_file_diff(
            "architecture/adr/ORG-998-ok.md",
            [
                "---", "id: ORG-998", "build_disposition: needs-build",
                "scoping_ticket: T-3001", "---", "", "# ORG-998",
            ],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_new_adr_form_prefix_recognized(self) -> None:
        diff = self._new_file_diff(
            "architecture/adr/ADR-500-gap.md",
            ["---", "id: ADR-500", "build_disposition: needs-build", "---", "", "# ADR-500"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/adr/ADR-500-gap.md", violations[0])

    def test_modified_existing_adr_not_flagged_creation_lint_only(self) -> None:
        """T-2806 §4 scope: this lint only inspects NEWLY ADDED files -- a
        modified existing (non-conformant) ADR is the backstop's job
        (tools/ticket_grooming.py's ratified-unbuilt-ADR check), not this
        lint's."""
        diff = (
            "diff --git a/architecture/adr/ORG-070-x.md b/architecture/adr/ORG-070-x.md\n"
            "index abc123..def456 100644\n"
            "--- a/architecture/adr/ORG-070-x.md\n"
            "+++ b/architecture/adr/ORG-070-x.md\n"
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
            "architecture/adr/ORG-997-policy.md",
            ["---", "id: ORG-997", "build_disposition: no-build-needed", "---", "", "# ORG-997"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(violations, [])

    def test_multiple_new_adrs_each_evaluated_independently(self) -> None:
        diff = self._new_file_diff(
            "architecture/adr/ORG-996-gap.md",
            ["---", "id: ORG-996", "build_disposition: needs-build", "---", "", "# ORG-996"],
        ) + self._new_file_diff(
            "architecture/adr/ORG-995-ok.md",
            [
                "---", "id: ORG-995", "build_disposition: needs-build",
                "scoping_ticket: T-3002", "---", "", "# ORG-995",
            ],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/adr/ORG-996-gap.md", violations[0])

    def test_empty_diff_no_violations(self) -> None:
        self.assertEqual(adr_scoping_lint.scan_diff_for_new_adrs(""), [])


class TestSharedFraming(unittest.TestCase):
    """T-4202: the lint rides the shared structural diff framing
    (hermes_diff_scan.iter_added_lines). Each of these failed on the old
    prefix-matching / splitlines() parser."""

    _new_file_diff = TestScanDiffForNewAdrs._new_file_diff

    def _captured(self, diff: "str | bytes") -> dict:
        """Run the scan, capturing the text each new ADR is judged on."""
        seen: dict = {}

        def capture(path, text):
            seen[path] = text
            return None

        with mock.patch.object(adr_scoping_lint, "check_adr_frontmatter", side_effect=capture):
            self.assertEqual(adr_scoping_lint.scan_diff_for_new_adrs(diff), [])
        return seen

    def test_plus_plus_space_body_line_does_not_bypass_the_lint(self) -> None:
        # Body line "++ foo" renders "+++ foo": the old parser took it for a
        # file header, re-pointed the path at "foo" and skipped the ADR.
        diff = self._new_file_diff(
            "architecture/adr/ORG-990-gap.md",
            ["---", "id: ORG-990", "build_disposition: needs-build", "---", "", "++ foo", "# ORG-990"],
        )
        violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/adr/ORG-990-gap.md", violations[0])

    def test_plus_prefixed_lines_are_kept_verbatim(self) -> None:
        body = ["---", "id: ORG-989", "---", "++x", "++ y", "+++z"]
        seen = self._captured(self._new_file_diff("architecture/adr/ORG-989-k.md", body))
        self.assertEqual(seen, {"architecture/adr/ORG-989-k.md": "\n".join(body)})

    def test_form_feed_and_u2028_do_not_split_a_line(self) -> None:
        body = ["---", "id: ORG-988", "---", "a\x0cb", "c d\u0085e"]
        seen = self._captured(self._new_file_diff("architecture/adr/ORG-988-k.md", body))
        self.assertEqual(seen, {"architecture/adr/ORG-988-k.md": "\n".join(body)})

    def test_u2028_line_cannot_end_the_frontmatter_early(self) -> None:
        # The old splitlines() turned "x ---" into a "---" line, closing
        # the frontmatter before scoping_ticket and flagging a compliant ADR.
        diff = self._new_file_diff(
            "architecture/adr/ORG-985-ok.md",
            ["---", "id: ORG-985", "title: \"a ---\"", "scoping_ticket: T-3003", "---"],
        )
        self.assertEqual(adr_scoping_lint.scan_diff_for_new_adrs(diff), [])

    def test_crlf_adr_frontmatter_still_read(self) -> None:
        diff = self._new_file_diff(
            "architecture/adr/ORG-984-ok.md",
            ["---\r", "id: ORG-984\r", "scoping_ticket: T-3004\r", "---\r"],
        )
        self.assertEqual(adr_scoping_lint.scan_diff_for_new_adrs(diff), [])

    def test_space_and_c_quoted_adr_paths_are_linted(self) -> None:
        body = ["---", "id: ORG-987", "build_disposition: needs-build", "---"]
        tab = self._new_file_diff("architecture/adr/ORG-987 sp.md", body).replace(
            "+++ b/architecture/adr/ORG-987 sp.md\n", "+++ b/architecture/adr/ORG-987 sp.md\t\n")
        quoted = self._new_file_diff("architecture/adr/ORG-986-x.md", body).replace(
            "+++ b/architecture/adr/ORG-986-x.md\n",
            '+++ "b/architecture/adr/ORG-986-caf\\303\\251.md"\n')
        for diff, want in ((tab, "architecture/adr/ORG-987 sp.md"),
                           (quoted, "architecture/adr/ORG-986-café.md")):
            with self.subTest(want=want):
                violations = adr_scoping_lint.scan_diff_for_new_adrs(diff)
                self.assertEqual(len(violations), 1, violations)
                self.assertIn(want, violations[0])


class TestSharedFramingRealGit(unittest.TestCase):
    """End to end over real `git diff` output, through the CLI's bytes stdin."""

    def _git(self, repo: Path, *args: str) -> bytes:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        return subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", *args],
            cwd=repo, env=env, capture_output=True, check=True,
        ).stdout

    def test_plus_plus_body_and_quoted_name_are_caught_via_cli(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t)
            self._git(repo, "init", "-q")
            (repo / "README").write_text("x\n")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "base")
            adr = repo / "architecture" / "adr"
            adr.mkdir(parents=True)
            (adr / "ORG-983-gap.md").write_text(
                "---\nid: ORG-983\nbuild_disposition: needs-build\n---\n\n++ foo\n")
            (adr / "ORG-982-café.md").write_text("---\nid: ORG-982\n---\n")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "adrs")
            diff = self._git(repo, "diff", "HEAD~1", "HEAD")
            self.assertIn(b"\n+++ foo\n", diff)
            res = subprocess.run(
                [sys.executable, str(Path(adr_scoping_lint.__file__))],
                input=diff, capture_output=True,
            )
        self.assertEqual(res.returncode, 1, res.stderr)
        err = res.stderr.decode()
        self.assertIn("architecture/adr/ORG-983-gap.md", err)
        self.assertIn("architecture/adr/ORG-982-café.md", err)


class TestMain(unittest.TestCase):
    """CLI entry point exit codes (mirrors tools.hermes_diff_scan's contract)."""

    def test_main_returns_zero_on_clean_diff(self) -> None:
        import io
        from unittest.mock import patch

        diff = (
            "diff --git a/architecture/adr/ORG-994-ok.md b/architecture/adr/ORG-994-ok.md\n"
            "new file mode 100644\n"
            "index 0000000..abc\n"
            "--- /dev/null\n"
            "+++ b/architecture/adr/ORG-994-ok.md\n"
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
            "diff --git a/architecture/adr/ORG-993-gap.md b/architecture/adr/ORG-993-gap.md\n"
            "new file mode 100644\n"
            "index 0000000..abc\n"
            "--- /dev/null\n"
            "+++ b/architecture/adr/ORG-993-gap.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+---\n"
            "+build_disposition: needs-build\n"
            "+---\n"
        )
        with patch("sys.stdin", io.StringIO(diff)), patch("sys.argv", ["adr_scoping_lint.py"]):
            rc = adr_scoping_lint.main()
        self.assertEqual(rc, 1)


class TestTreeFrontmatterParses(unittest.TestCase):
    """T-4692: every ADR/RFC frontmatter in the tree must parse as YAML. A
    status flip on an EXISTING ADR (#748, an unquoted ': ') is invisible to the
    creation-only diff lint, so this is a state check over the checkout."""

    def _tree(self, files: dict[str, str]) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return root

    def test_unquoted_colon_in_modified_adr_is_flagged(self) -> None:
        root = self._tree({
            "architecture/adr/ORG-900-x.md": "---\nid: ORG-900\nstatus: Ratified 2026-10-07: TG 1\n---\nbody\n",
        })
        violations = adr_scoping_lint.scan_tree_frontmatter(root)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/adr/ORG-900-x.md", violations[0])

    def test_rfc_dir_is_scanned(self) -> None:
        root = self._tree({
            "architecture/rfc/RFC-009-x.md": "---\nstatus: Draft: v2\n---\n",
        })
        violations = adr_scoping_lint.scan_tree_frontmatter(root)
        self.assertEqual(len(violations), 1)
        self.assertIn("architecture/rfc/RFC-009-x.md", violations[0])

    def test_valid_and_absent_frontmatter_pass(self) -> None:
        root = self._tree({
            "architecture/adr/ORG-901-ok.md": "---\nstatus: \"Ratified: yes\"\n---\n",
            "architecture/adr/README.md": "# no frontmatter\n",
            "architecture/rfc/RFC-010-ok.md": "---\nid: RFC-010\n---\n",
        })
        self.assertEqual(adr_scoping_lint.scan_tree_frontmatter(root), [])

    def test_main_tree_mode_exit_codes(self) -> None:
        bad = self._tree({"architecture/adr/ORG-902-x.md": "---\na: b: c\n---\n"})
        good = self._tree({"architecture/adr/ORG-903-x.md": "---\na: b\n---\n"})
        with mock.patch("sys.argv", ["adr_scoping_lint.py", "--tree", str(bad)]):
            self.assertEqual(adr_scoping_lint.main(), 1)
        with mock.patch("sys.argv", ["adr_scoping_lint.py", "--tree", str(good)]):
            self.assertEqual(adr_scoping_lint.main(), 0)

    def test_live_tree_parses(self) -> None:
        self.assertEqual(adr_scoping_lint.scan_tree_frontmatter(adr_scoping_lint._REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
