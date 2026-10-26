"""Unit tests for tools/directive_skill_drift_check.py — the T-0796 directive<->skill drift guard.

Run from repo root:
    python3 -m unittest tools.test_directive_skill_drift_check -v

Covers:
  - directive_ids: parses `## DN` headers, ignores inline D-mentions, honors header levels
  - skill_ids: extracts every Dn token from skill text
  - resolve_skill: --skill arg > $ORG_SKILL_PATH > known fallbacks > None
  - main: in-sync (0), drift (1), skill-not-found (0 + warn)

No side effects in this tool (pure file-read + compare + exit code), so no mocking of
network/subprocess is needed — only tmp files + module-global redirection.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.directive_skill_drift_check as dsd


class DirectiveIdsTest(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        Path(path).write_text(text, encoding="utf-8")
        self.addCleanup(os.unlink, path)
        return path

    def test_parses_dn_headers(self):
        path = self._write("## D1 — first\n## D2 — second\n### D11 — eleventh\n")
        self.assertEqual(dsd.directive_ids(path), {1, 2, 11})

    def test_ignores_inline_d_mentions(self):
        # A D-token in body prose is NOT a header; only `#+ DN` headers count as the source set.
        path = self._write("## D1 — real\nsome prose citing D7 and D9 inline\n")
        self.assertEqual(dsd.directive_ids(path), {1})

    def test_honors_varying_header_levels(self):
        path = self._write("# D3 — h1\n#### D4 — h4\n")
        self.assertEqual(dsd.directive_ids(path), {3, 4})

    def test_empty_file_yields_empty_set(self):
        path = self._write("no directives here\n")
        self.assertEqual(dsd.directive_ids(path), set())


class SkillIdsTest(unittest.TestCase):
    def _write(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        Path(path).write_text(text, encoding="utf-8")
        self.addCleanup(os.unlink, path)
        return path

    def test_extracts_all_dn_tokens_anywhere(self):
        # Unlike directive_ids, skill_ids counts Dn tokens ANYWHERE (the skill mentions them in prose).
        path = self._write("This skill applies D1, D2 and also D12 where relevant.\n")
        self.assertEqual(dsd.skill_ids(path), {1, 2, 12})

    def test_no_tokens_yields_empty_set(self):
        path = self._write("no directive tokens\n")
        self.assertEqual(dsd.skill_ids(path), set())


class ResolveSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # Neutralize real fallbacks + env so resolution is deterministic.
        self._orig_fallbacks = dsd.SKILL_FALLBACKS
        dsd.SKILL_FALLBACKS = [os.path.join(self.tmp, "nonexistent-fallback.md")]
        self.addCleanup(setattr, dsd, "SKILL_FALLBACKS", self._orig_fallbacks)

    def _touch(self, name: str) -> str:
        path = os.path.join(self.tmp, name)
        Path(path).write_text("x", encoding="utf-8")
        return path

    def test_arg_wins_when_it_exists(self):
        arg = self._touch("from_arg.md")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            # resolve_skill now returns (path, configured) per ORG-039 §4.
            self.assertEqual(dsd.resolve_skill(arg), (arg, arg))

    def test_falls_to_env_when_no_arg(self):
        env_skill = self._touch("from_env.md")
        with patch.dict(os.environ, {"ORG_SKILL_PATH": env_skill}):
            self.assertEqual(dsd.resolve_skill(None), (env_skill, env_skill))

    def test_falls_to_fallback(self):
        fb = self._touch("fb.md")
        dsd.SKILL_FALLBACKS = [fb]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            # fallback path resolves, but nothing was explicitly *configured*.
            self.assertEqual(dsd.resolve_skill(None), (fb, None))

    def test_none_when_nothing_resolves(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            # not configured -> (None, None): a clean fork, drift check skips cleanly.
            self.assertEqual(dsd.resolve_skill(None), (None, None))

    def test_configured_but_absent_is_reported(self):
        # A path explicitly configured but missing -> (None, configured): actionable SKIP.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            self.assertEqual(dsd.resolve_skill("/no/such/path.md"), (None, "/no/such/path.md"))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        # Redirect the canonical directives path to a controlled fixture.
        self.directives = os.path.join(self.tmp, "directives.md")
        Path(self.directives).write_text("## D1 — a\n## D2 — b\n## D3 — c\n", encoding="utf-8")
        self._orig_dir = dsd.DIRECTIVES
        dsd.DIRECTIVES = self.directives
        self.addCleanup(setattr, dsd, "DIRECTIVES", self._orig_dir)
        # Neutralize real fallbacks/env so a real skill copy on the host never leaks in.
        self._orig_fallbacks = dsd.SKILL_FALLBACKS
        dsd.SKILL_FALLBACKS = [os.path.join(self.tmp, "nope.md")]
        self.addCleanup(setattr, dsd, "SKILL_FALLBACKS", self._orig_fallbacks)

    def _run_main(self, argv):
        buf = io.StringIO()
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            with redirect_stdout(buf):
                rc = dsd.main()
        return rc, buf.getvalue()

    def test_in_sync_returns_0(self):
        skill = os.path.join(self.tmp, "skill.md")
        Path(skill).write_text("covers D1 D2 D3\n", encoding="utf-8")
        rc, out = self._run_main(["prog", f"--skill={skill}"])
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)

    def test_drift_returns_1(self):
        skill = os.path.join(self.tmp, "skill.md")
        Path(skill).write_text("covers D1 D2 only\n", encoding="utf-8")  # missing D3
        rc, out = self._run_main(["prog", f"--skill={skill}"])
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("D3", out)

    def test_configured_but_absent_skips_with_actionable_message(self):
        # --skill points at a missing file: explicit, actionable SKIP, exit 0 (ORG-039 §4).
        rc, out = self._run_main(["prog", "--skill=/no/such/skill.md"])
        self.assertEqual(rc, 0)
        self.assertIn("SKIP", out)
        self.assertIn("/no/such/skill.md", out)

    def test_unconfigured_fork_skips_cleanly(self):
        # No --skill / $ORG_SKILL_PATH and no fallback present: clean-fork SKIP, exit 0.
        saved = dsd.SKILL_FALLBACKS
        dsd.SKILL_FALLBACKS = ["/no/such/fallback.md"]
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ORG_SKILL_PATH", None)
                rc, out = self._run_main(["prog"])
            self.assertEqual(rc, 0)
            self.assertIn("SKIP", out)
            self.assertIn("no harness-config repo configured", out)
        finally:
            dsd.SKILL_FALLBACKS = saved

    def test_space_separated_skill_arg(self):
        skill = os.path.join(self.tmp, "skill.md")
        Path(skill).write_text("covers D1 D2 D3\n", encoding="utf-8")
        rc, out = self._run_main(["prog", "--skill", skill])
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)


class InRepoHarnessTest(unittest.TestCase):
    """T-4763 (ORG-091): the in-repo harness/claude skill is the FIRST fallback, ahead of
    the frozen ~/claude-config clone, and it is present and in sync in this repo."""

    def test_in_repo_harness_is_first_fallback(self):
        self.assertEqual(
            Path(dsd.SKILL_FALLBACKS[0]),
            REPO_ROOT / "harness" / "claude" / "skills" / "pantheon-org-discipline" / "SKILL.md")
        frozen = os.path.expanduser("~/claude-config/skills/pantheon-org-discipline/SKILL.md")
        self.assertGreater(dsd.SKILL_FALLBACKS.index(frozen), 0)

    def test_unconfigured_run_resolves_the_in_repo_skill(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            self.assertEqual(dsd.resolve_skill(None), (dsd.SKILL_FALLBACKS[0], None))

    def test_real_directives_are_covered_by_the_in_repo_skill(self):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["prog", f"--skill={dsd.SKILL_FALLBACKS[0]}"]), \
             redirect_stdout(buf):
            rc = dsd.main()
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("OK", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
