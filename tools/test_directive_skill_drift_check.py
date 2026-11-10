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
    """T-4763 (ORG-091): the in-repo harness/claude skill is the FIRST fallback. T-4842: the
    frozen ~/claude-config clone and the never-existing REPO/claude-config path are gone."""

    def test_in_repo_harness_is_first_fallback(self):
        self.assertEqual(
            Path(dsd.SKILL_FALLBACKS[0]),
            REPO_ROOT / "harness" / "claude" / "skills" / "pantheon-org-discipline" / "SKILL.md")
        self.assertFalse(any("claude-config" in c for c in dsd.SKILL_FALLBACKS))

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


class ConfiguredPathNeverFallsThroughTest(unittest.TestCase):
    """T-4842: a configured (--skill / $ORG_SKILL_PATH) but missing path must SKIP, never
    fall through to a fallback and report OK against a file the seat does not load."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.directives = os.path.join(self.tmp, "directives.md")
        Path(self.directives).write_text("## D1 - a\n## D2 - b\n", encoding="utf-8")
        # A PRESENT, in-sync fallback: falling through to it would print OK.
        self.fallback = os.path.join(self.tmp, "fallback.md")
        Path(self.fallback).write_text("D1 D2\n", encoding="utf-8")
        self.good = os.path.join(self.tmp, "good.md")
        Path(self.good).write_text("D1 D2\n", encoding="utf-8")
        self.bad = os.path.join(self.tmp, "bad.md")
        Path(self.bad).write_text("D1 only\n", encoding="utf-8")
        self.missing = os.path.join(self.tmp, "missing.md")
        for name, val in (("DIRECTIVES", self.directives), ("SKILL_FALLBACKS", [self.fallback])):
            orig = getattr(dsd, name)
            setattr(dsd, name, val)
            self.addCleanup(setattr, dsd, name, orig)

    def _run(self, argv, env=None):
        buf = io.StringIO()
        with patch.object(sys, "argv", ["prog", *argv]), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORG_SKILL_PATH", None)
            if env:
                os.environ["ORG_SKILL_PATH"] = env
            with redirect_stdout(buf):
                rc = dsd.main()
        return rc, buf.getvalue()

    def _assert_configured_skip(self, rc, out, path):
        self.assertEqual(rc, 0)
        self.assertIn("SKIP", out)
        self.assertNotIn("OK", out)
        self.assertIn(path, out)
        self.assertNotIn(self.fallback, out)
        self.assertIn("configured harness skill path not found", out)
        self.assertNotIn("no harness-config repo configured", out)

    def test_missing_skill_arg_skips_not_fallback(self):
        rc, out = self._run([f"--skill={self.missing}"])
        self._assert_configured_skip(rc, out, self.missing)

    def test_missing_env_skips_not_fallback(self):
        rc, out = self._run([], env=self.missing)
        self._assert_configured_skip(rc, out, self.missing)

    def test_missing_skill_arg_beats_existing_env(self):
        # --skill wins over $ORG_SKILL_PATH; it is missing, so SKIP (not the env file).
        rc, out = self._run([f"--skill={self.missing}"], env=self.good)
        self._assert_configured_skip(rc, out, self.missing)

    def test_existing_skill_arg_beats_missing_env(self):
        rc, out = self._run([f"--skill={self.good}"], env=self.missing)
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)
        self.assertIn(self.good, out)

    def test_existing_configured_path_drift_is_checked_on_that_file(self):
        rc, out = self._run([f"--skill={self.bad}"])
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", out)
        self.assertIn(self.bad, out)

    def test_existing_env_path_is_checked(self):
        rc, out = self._run([], env=self.good)
        self.assertEqual(rc, 0)
        self.assertIn(self.good, out)

    def test_nothing_configured_uses_fallback(self):
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)
        self.assertIn(self.fallback, out)

    def test_nothing_configured_no_fallback_is_not_configured_skip(self):
        dsd.SKILL_FALLBACKS = [self.missing]
        rc, out = self._run([])
        self.assertEqual(rc, 0)
        self.assertIn("no harness-config repo configured", out)
        self.assertNotIn("configured harness skill path not found", out)

    def test_home_claude_config_is_not_consulted(self):
        # Real fallbacks recomputed with HOME pointing at a tmp dir holding the old clone's skill.
        import importlib
        home = os.path.join(self.tmp, "home")
        old = os.path.join(home, "claude-config", "skills", "pantheon-org-discipline")
        os.makedirs(old)
        stale = os.path.join(old, "SKILL.md")
        Path(stale).write_text("D1 D2\n", encoding="utf-8")
        try:
            with patch.dict(os.environ, {"HOME": home}):
                importlib.reload(dsd)
                self.assertNotIn(stale, dsd.SKILL_FALLBACKS)
                self.assertFalse(any("claude-config" in c for c in dsd.SKILL_FALLBACKS))
        finally:
            importlib.reload(dsd)


if __name__ == "__main__":
    unittest.main()
