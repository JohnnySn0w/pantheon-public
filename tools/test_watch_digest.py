"""Tests for tools.watch_digest — pull_repo_or_warn fanout (T-0120)."""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import watch_digest


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics for watch_digest — never raises, never blocks."""

    def test_pull_success_silent(self):
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            watch_digest.pull_repo_or_warn("watch_digest")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import contextlib
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_digest.pull_repo_or_warn("watch_digest")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import contextlib
        import subprocess as real_sub
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                watch_digest.pull_repo_or_warn("watch_digest")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


class TestSurfacing(unittest.TestCase):
    """Producer-declared `surface:` frontmatter is authoritative; keyword scan is fallback only."""

    def test_frontmatter_above_bar_surfaces_and_uses_headline(self):
        r = "---\nsurface: above-bar\nheadline: New MCP CVE hits Claude Code clients\n---\n# Report\nbody"
        self.assertTrue(watch_digest.is_above_bar(r))
        self.assertEqual(watch_digest.report_summary(r), "New MCP CVE hits Claude Code clients")

    def test_routine_overrides_keyword_in_body(self):
        # The core fix: a self-declared routine report does NOT surface even if its
        # body mentions a fast-path keyword in passing (old keyword-only false positive).
        r = "---\nsurface: routine\nheadline: Nothing material this week\n---\nbody mentions billing in passing"
        self.assertFalse(watch_digest.is_above_bar(r))
        self.assertEqual(watch_digest.report_summary(r), "Nothing material this week")

    def test_legacy_no_frontmatter_keyword_fallback(self):
        r = "# Old report\nThere is a new CVE-2026-1234 affecting things"
        self.assertTrue(watch_digest.is_above_bar(r))

    def test_legacy_no_signal_is_routine_with_first_line_summary(self):
        r = "# Gas City\nGas City is Gas Town rewritten as an SDK."
        self.assertFalse(watch_digest.is_above_bar(r))
        self.assertIn("SDK", watch_digest.report_summary(r))

    def test_parse_frontmatter_absent(self):
        self.assertEqual(watch_digest.parse_frontmatter("# no frontmatter\nbody"), {})


class TestSendTelegramFallback(unittest.TestCase):
    """send_telegram must fall back to plain text when Markdown parsing 400s.

    Regression for 2026-06-08: an unbalanced `_` (in a `watch_runner` headline,
    one of 8 above-bar items) made legacy-Markdown parsing 400, dropping the whole
    digest — the most important one of the week. Headlines are arbitrary researcher
    prose, so delivery must not depend on them being Markdown-clean.
    """

    def _resp(self, status):
        m = MagicMock()
        m.status = status
        m.__enter__ = lambda s: m
        m.__exit__ = lambda s, *a: False
        return m

    def test_markdown_400_falls_back_to_plain_text(self):
        import urllib.error
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.data.decode("utf-8"))
            if len(calls) == 1:  # first (Markdown) attempt 400s
                raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)
            return self._resp(200)  # plain-text retry succeeds

        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", "x with a lone _ underscore")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertIn("parse_mode", calls[0])          # 1st try used Markdown
        self.assertNotIn("parse_mode", calls[1])       # fallback dropped parse_mode

    def test_markdown_success_no_fallback(self):
        def fake_urlopen(req, timeout=None):
            return self._resp(200)

        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=fake_urlopen) as mock_open:
            ok = watch_digest.send_telegram("tok", "123", "clean text")
        self.assertTrue(ok)
        self.assertEqual(mock_open.call_count, 1)       # no retry when Markdown works

    def test_both_attempts_fail_returns_false(self):
        with patch("tools.watch_digest.urllib.request.urlopen", side_effect=OSError("net down")), \
             patch("sys.stderr", new_callable=io.StringIO):
            ok = watch_digest.send_telegram("tok", "123", "text")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
