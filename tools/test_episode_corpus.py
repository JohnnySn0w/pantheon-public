"""Tri-state committed-corpus lookup (T-4707 amendments A/B): FOUND / ABSENT / UNKNOWN."""
from __future__ import annotations

import unittest

from tools import _episode_corpus as ec
from tools._episode_fixtures import RepoFixture, git


def _commit_ticket(repo, name, episode_id, push=False):
    p = repo / "tickets" / "inbox" / f"{name}.md"
    p.write_text(f"---\nid: {name}\nepisode_id: {episode_id}\n---\n\n# x\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", f"file {name}", cwd=repo)
    if push:
        git("push", "-q", "origin", "HEAD:main", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


class CorpusLookup(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.repo = self.fx.repo

    def test_absent(self):
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_working_tree_only_is_absent(self):
        (self.repo / "tickets" / "inbox" / "T-1.md").write_text("---\nid: T-1\nepisode_id: ep-1\n---\n")
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_staged_only_is_absent(self):
        (self.repo / "tickets" / "inbox" / "T-1.md").write_text("---\nid: T-1\nepisode_id: ep-1\n---\n")
        git("add", "-A", cwd=self.repo)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_local_ahead_found_with_sha(self):
        sha = _commit_ticket(self.repo, "T-1", "ep-1")
        r = ec.lookup_episode("ep-1", repo=self.repo)
        self.assertEqual((r.status, r.where, r.sha), (ec.FOUND, "local-ahead", sha))

    def test_origin_found(self):
        _commit_ticket(self.repo, "T-1", "ep-1", push=True)
        r = ec.lookup_episode("ep-1", repo=self.repo)
        self.assertEqual((r.status, r.where), (ec.FOUND, "origin"))
        self.assertIn("T-1.md", r.detail)

    def test_prefix_id_does_not_match(self):
        _commit_ticket(self.repo, "T-1", "ep-12", push=True)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_fetch_failure_is_unknown_not_absent(self):
        git("remote", "set-url", "origin", str(self.fx.root / "gone.git"), cwd=self.repo)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.UNKNOWN)

    def test_bad_ref_is_unknown_not_absent(self):
        git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.repo)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo, fetch=False).status, ec.UNKNOWN)

    def test_not_a_repo_is_unknown(self):
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.fx.root / "locks", fetch=False).status, ec.UNKNOWN)


class FrontmatterAnchoring(unittest.TestCase):
    """Recovery note (a): only a FRONTMATTER ``episode_id:`` line counts as delivered."""

    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.repo = self.fx.repo

    def _body_only(self, push):
        p = self.repo / "tickets" / "inbox" / "T-9.md"
        p.write_text("---\nid: T-9\n---\n\n## Context\n\nepisode_id: ep-1\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "body only", cwd=self.repo)
        if push:
            git("push", "-q", "origin", "HEAD:main", cwd=self.repo)

    def test_body_only_line_is_absent_on_origin(self):
        self._body_only(push=True)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_body_only_line_is_absent_local_ahead(self):
        self._body_only(push=False)
        self.assertEqual(ec.lookup_episode("ep-1", repo=self.repo).status, ec.ABSENT)

    def test_frontmatter_line_is_found_origin_and_local_ahead(self):
        sha = _commit_ticket(self.repo, "T-1", "ep-1")
        r = ec.lookup_episode("ep-1", repo=self.repo)
        self.assertEqual((r.status, r.where, r.sha), (ec.FOUND, "local-ahead", sha))
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        r = ec.lookup_episode("ep-1", repo=self.repo)
        self.assertEqual((r.status, r.where), (ec.FOUND, "origin"))

    def test_helper_semantics(self):
        self.assertTrue(ec.frontmatter_has_episode("---\nid: T-1\nepisode_id: ep-1\n---\nbody\n", "ep-1"))
        self.assertFalse(ec.frontmatter_has_episode("---\nid: T-1\n---\nepisode_id: ep-1\n", "ep-1"))
        self.assertFalse(ec.frontmatter_has_episode("---\nepisode_id: ep-12\n---\n", "ep-1"))


class FrontmatterEdgeCases(unittest.TestCase):
    """BT3a review B3: no-frontmatter, CRLF, fence-at-EOF, BOM, prefix ids."""

    def test_no_frontmatter_is_false(self):
        self.assertFalse(ec.frontmatter_has_episode("# T\nepisode_id: ep-1\n", "ep-1"))

    def test_crlf_body_line_is_false(self):
        self.assertFalse(ec.frontmatter_has_episode("---\r\nid: T-1\r\n---\r\n\r\nepisode_id: ep-1\r\n", "ep-1"))

    def test_crlf_frontmatter_line_is_true(self):
        self.assertTrue(ec.frontmatter_has_episode("---\r\nid: T-1\r\nepisode_id: ep-1\r\n---\r\nbody\r\n", "ep-1"))

    def test_closing_fence_at_eof(self):
        self.assertTrue(ec.frontmatter_has_episode("---\nepisode_id: ep-1\n---", "ep-1"))
        self.assertTrue(ec.frontmatter_has_episode("---\nepisode_id: ep-1\n---\n", "ep-1"))

    def test_unterminated_frontmatter_is_false(self):
        self.assertFalse(ec.frontmatter_has_episode("---\nepisode_id: ep-1\nmore\n", "ep-1"))

    def test_bom_tolerated(self):
        self.assertTrue(ec.frontmatter_has_episode("\ufeff---\nepisode_id: ep-1\n---\n", "ep-1"))

    def test_prefix_id_does_not_match(self):
        self.assertFalse(ec.frontmatter_has_episode("---\nepisode_id: ep-12\n---\n", "ep-1"))
        self.assertTrue(ec.frontmatter_has_episode("---\nepisode_id: ep-12\n---\n", "ep-12"))

    def test_empty_frontmatter_then_body_line_is_false(self):
        self.assertFalse(ec.frontmatter_has_episode("---\n---\nepisode_id: ep-1\n", "ep-1"))

    def test_episode_store_delegate_inherits_the_anchoring(self):
        import tempfile
        from pathlib import Path
        from tools import episode_store as es
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "T-1.md"
            p.write_text("# T\nepisode_id: ep-1\n")
            self.assertFalse(es._frontmatter_has_episode(p, "ep-1"))
            p.write_text("---\r\nepisode_id: ep-1\r\n---\r\n")
            self.assertTrue(es._frontmatter_has_episode(p, "ep-1"))


if __name__ == "__main__":
    unittest.main()
