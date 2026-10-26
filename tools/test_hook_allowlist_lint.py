"""Unit tests for tools/hook_allowlist_lint.py — the T-1102 hook<->allowlist pairing guard.

Run from repo root:
    python3 -m unittest tools.test_hook_allowlist_lint -v

The lint deliberately REUSES context_fill_monitor.check_hook_allowlist (already
covered by test_context_fill_monitor), so these tests verify the WIRING — agent
detection, the no-agent/unknown-agent short-circuits, drift -> exit 1, clean ->
exit 0, and the fail-OPEN-on-crash posture — not the predicate internals.
"""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.hook_allowlist_lint as hal


class LocalAgentTest(unittest.TestCase):
    def test_maps_known_user_to_agent(self):
        # AGENT_CONFIG maps agent->{sudo_user}; local_agent inverts it.
        with patch("tools.hook_allowlist_lint.getpass.getuser", return_value="claude-fallback"):
            self.assertEqual(hal.local_agent(), "recovery")
        with patch("tools.hook_allowlist_lint.getpass.getuser", return_value="claude"):
            self.assertEqual(hal.local_agent(), "orchestrator")

    def test_explicit_user_arg_overrides(self):
        self.assertEqual(hal.local_agent("claude-research"), "specialist")

    def test_unknown_user_is_none(self):
        self.assertIsNone(hal.local_agent("some-random-ci-user"))


class LintTest(unittest.TestCase):
    def test_returns_deviations_from_check(self):
        with patch("tools.hook_allowlist_lint.check_hook_allowlist", return_value=["evil.sh"]) as chk:
            self.assertEqual(hal.lint("recovery"), ["evil.sh"])
            chk.assert_called_once()

    def test_clean_when_check_returns_empty(self):
        with patch("tools.hook_allowlist_lint.check_hook_allowlist", return_value=[]):
            self.assertEqual(hal.lint("recovery"), [])

    def test_no_agent_short_circuits_without_calling_check(self):
        with patch("tools.hook_allowlist_lint.local_agent", return_value=None), patch(
            "tools.hook_allowlist_lint.check_hook_allowlist"
        ) as chk:
            self.assertEqual(hal.lint(), [])
            chk.assert_not_called()

    def test_agent_absent_from_allowlist_short_circuits(self):
        # An agent with no HOOK_ALLOWLIST entry must not call the checker (it would
        # be a config-drift no-op there anyway) — lint returns [] cleanly.
        with patch.dict("tools.hook_allowlist_lint.HOOK_ALLOWLIST", {}, clear=True), patch(
            "tools.hook_allowlist_lint.check_hook_allowlist"
        ) as chk:
            self.assertEqual(hal.lint("recovery"), [])
            chk.assert_not_called()


class MainTest(unittest.TestCase):
    def _run(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = hal.main()
        return rc, buf.getvalue()

    def test_clean_returns_0(self):
        with patch("tools.hook_allowlist_lint.local_agent", return_value="recovery"), patch(
            "tools.hook_allowlist_lint.lint", return_value=[]
        ):
            rc, _ = self._run()
            self.assertEqual(rc, 0)

    def test_drift_returns_1_with_fix_message(self):
        with patch("tools.hook_allowlist_lint.local_agent", return_value="recovery"), patch(
            "tools.hook_allowlist_lint.lint", return_value=["new-hook.py"]
        ):
            rc, err = self._run()
            self.assertEqual(rc, 1)
            self.assertIn("new-hook.py", err)
            self.assertIn("HOOK_ALLOWLIST", err)
            self.assertIn("T-1102", err)

    def test_failopen_on_unexpected_error(self):
        # A crash inside the lint must NOT brick commits -> exit 0.
        with patch("tools.hook_allowlist_lint.local_agent", side_effect=RuntimeError("boom")):
            rc, err = self._run()
            self.assertEqual(rc, 0)
            self.assertIn("fail-open", err)

    def test_non_agent_committer_returns_0(self):
        with patch("tools.hook_allowlist_lint.local_agent", return_value=None):
            rc, _ = self._run()
            self.assertEqual(rc, 0)


class FormatFailureTest(unittest.TestCase):
    def test_contains_agent_deviations_and_fix(self):
        msg = hal.format_failure("recovery", ["a.sh", "b.py"])
        self.assertIn("recovery", msg)
        self.assertIn("a.sh", msg)
        self.assertIn("b.py", msg)
        self.assertIn("do NOT auto-derive", msg)
        self.assertIn("--no-verify", msg)


class PluginDirT4577Test(unittest.TestCase):
    """T-4577: mods loaded via settings.json env CLAUDE_CODE_PLUGIN_DIRS."""

    def test_failure_message_names_the_plugin_allowlist(self):
        dev = hal.PLUGIN_DEVIATION_PREFIX + "'/tmp/evil' (not an allowlisted in-repo mod dir)"
        msg = hal.format_failure("recovery", [dev])
        self.assertIn("PLUGIN_DIR_ALLOWLIST['recovery']", msg)
        self.assertIn("--no-verify", msg)
        self.assertNotIn("PLUGIN_DIR_ALLOWLIST", hal.format_failure("recovery", ["a.sh"]))

    def test_every_repo_mod_keeps_its_modules_inside_its_dir(self):
        """The allowlist trusts tools/mods/<name> as reviewed code, so each mod's
        hooks.json modules must resolve inside its own dir, with no symlink anywhere
        in it that could point a reviewed name at unreviewed code."""
        import json
        import os
        mods = REPO_ROOT / "tools" / "mods"
        self.assertTrue(any(mods.iterdir()), "no mods: this guard would be vacuous")
        for mod in sorted(p for p in mods.iterdir() if p.is_dir()):
            with self.subTest(mod=mod.name):
                for root, dirs, files in os.walk(mod):
                    for name in dirs + files:
                        self.assertFalse(os.path.islink(os.path.join(root, name)),
                                         f"symlink in mod: {os.path.join(root, name)}")
                hooks = mod / "hooks" / "hooks.json"
                self.assertTrue(hooks.is_file(), f"{mod.name}: no hooks/hooks.json")
                modules = json.loads(hooks.read_text())["modules"]
                self.assertTrue(modules)
                for m in modules:
                    target = Path(os.path.normpath(hooks.parent / m))
                    self.assertTrue(target.is_file(), f"{mod.name}: {m} missing")
                    self.assertTrue(str(target).startswith(str(mod) + os.sep),
                                    f"{mod.name}: {m} escapes the mod dir")

    def test_every_allowlisted_mod_name_exists_in_the_repo(self):
        mods = REPO_ROOT / "tools" / "mods"
        for agent, names in hal.PLUGIN_DIR_ALLOWLIST.items():
            for name in names:
                self.assertTrue((mods / name).is_dir(), f"{agent}: {name} not in tools/mods/")


class RealEnvSmokeTest(unittest.TestCase):
    """Exercises the real predicate against the real local env (no mocks).

    Recovery's own wired hooks should all be allowlisted, so lint() is clean. This is
    the live invariant: green lint <=> the runtime T-0536 checker won't flag us.
    """

    def test_local_agent_resolves_and_lint_runs(self):
        agent = hal.local_agent()
        # In this fleet the committer is always one of the agents; if somehow not,
        # lint must still be safe ([]).
        result = hal.lint(agent)
        self.assertIsInstance(result, list)


if __name__ == "__main__":
    unittest.main()
