"""Tests for vault_link_check.py (T-1597). Run: python3 -m unittest tools.test_vault_link_check"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import vault_link_check as vlc


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ExtractLinks(unittest.TestCase):
    def test_basic_and_aliased_and_anchored(self):
        text = "See [[T-0010]] and [[ORG-020|the restart ADR]] and [[notes#Section]]."
        self.assertEqual(vlc.extract_links(text), ["T-0010", "ORG-020", "notes"])

    def test_fenced_code_blocks_skipped(self):
        text = "before [[real]]\n```\n[[not-a-link]]\n```\nafter [[also-real]]"
        self.assertEqual(vlc.extract_links(text), ["real", "also-real"])

    def test_inline_code_spans_skipped(self):
        text = "the syntax `[[hooks.<Event>]]` is literal but [[actual]] links"
        self.assertEqual(vlc.extract_links(text), ["actual"])


class FrontmatterAliases(unittest.TestCase):
    def test_inline_list(self):
        text = "---\ntitle: x\naliases: [ORG-020, ORG-20]\n---\nbody"
        self.assertEqual(vlc.frontmatter_aliases(text), ["ORG-020", "ORG-20"])

    def test_dash_list(self):
        text = "---\naliases:\n  - ORG-021\n  - alt-name\n---\n"
        self.assertEqual(vlc.frontmatter_aliases(text), ["ORG-021", "alt-name"])

    def test_no_frontmatter(self):
        self.assertEqual(vlc.frontmatter_aliases("# just a doc"), [])


class RunEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_resolved_by_basename_anywhere(self):
        _write(self.root, "tickets/done/T-0010.md", "a ticket")
        _write(self.root, "notes/x.md", "see [[T-0010]]")
        self.assertEqual(vlc.run(self.root), 0)

    def test_resolved_by_alias(self):
        _write(self.root, "adr/ORG-020-long-slug.md", "---\naliases: [ORG-020]\n---\nbody")
        _write(self.root, "notes/x.md", "see [[ORG-020]]")
        self.assertEqual(vlc.run(self.root), 0)

    def test_memory_pointer_is_not_dead(self):
        _write(self.root, "notes/x.md", "see [[feedback_some_lesson]] and [[reference_thing]]")
        self.assertEqual(vlc.run(self.root), 0)

    def test_dead_link_fails(self):
        _write(self.root, "notes/x.md", "see [[no-such-file]]")
        self.assertEqual(vlc.run(self.root), 1)

    def test_resolved_by_vault_relative_path(self):
        _write(self.root, "notes/2026-05-10-firm.md", "doc")
        _write(self.root, "adr/y.md", "see [[notes/2026-05-10-firm.md]] and [[notes/2026-05-10-firm]]")
        self.assertEqual(vlc.run(self.root), 0)

    def test_cross_repo_citation_annotation_allowed(self):
        _write(
            self.root, "notes/x.md",
            "see [[ADR-212]] and [[adr/ADR-210 — routing]] and "
            "[[Supplementary Material/ADRs/ADR-002 — lifecycle]] and [[3]] and "
            "[[single-source: forum thread]]",
        )
        self.assertEqual(vlc.run(self.root), 0)

    def test_dead_in_historical_dir_warns_not_fails(self):
        _write(self.root, "tickets/done/T-0001.md", "see [[gone-forever]]")
        _write(self.root, "knowledge/watch/reports/2026-06-22/r.md", "see [[also-gone]]")
        self.assertEqual(vlc.run(self.root), 0)

    def test_dead_in_curated_dir_fails_even_with_historical_elsewhere(self):
        _write(self.root, "tickets/done/T-0001.md", "see [[gone-forever]]")
        _write(self.root, "rulebook/policy.md", "see [[really-dead]]")
        self.assertEqual(vlc.run(self.root), 1)

    def test_obsidian_dir_skipped(self):
        _write(self.root, ".obsidian/whatever.md", "[[ghost]]")
        _write(self.root, "notes/x.md", "clean")
        self.assertEqual(vlc.run(self.root), 0)


if __name__ == "__main__":
    unittest.main()
