"""Tests for tools/seat_launch_config.py (T-4085). Run: python3 -m unittest tools.test_seat_launch_config"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import tools.seat_launch_config as slc

_ROLE_YAML = """\
harness_runtime:
  roles:
    engineering-orchestrator-1:
      claude:
        profile: interactive-subscription
        model: {model}
        native:
          permission_mode: bypassPermissions
{effort_line}
"""


class RoleLaunchOverridesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "instance.yaml"

    def _write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def _role(self, model: str = "claude-opus-5-5", effort: str | None = None) -> Path:
        effort_line = f"          effort: {effort}" if effort else ""
        return self._write(_ROLE_YAML.format(model=model, effort_line=effort_line))

    def test_reads_model_and_effort_for_persona_role(self) -> None:
        cfg = slc.role_launch_overrides("mason", self._role(effort="xhigh"))
        self.assertEqual(cfg, {"model": "claude-opus-5-5", "effort": "xhigh"})

    def test_undeclared_effort_is_none(self) -> None:
        cfg = slc.role_launch_overrides("mason", self._role())
        self.assertEqual(cfg, {"model": "claude-opus-5-5", "effort": None})

    def test_null_model_is_none_keep_captured(self) -> None:
        cfg = slc.role_launch_overrides("mason", self._role(model="null"))
        self.assertIsNone(cfg["model"])

    def test_role_without_claude_block_declares_nothing(self) -> None:
        path = self._write("harness_runtime:\n  roles:\n    engineering-orchestrator-1:\n      tmux: {session: s}\n")
        self.assertEqual(slc.role_launch_overrides("mason", path), {"model": None, "effort": None})

    def test_missing_role_raises(self) -> None:
        with self.assertRaises(slc.LaunchConfigError):
            slc.role_launch_overrides("artisan", self._role())   # only -1 present

    def test_missing_or_malformed_runtime_raises(self) -> None:
        for text in ("{}\n", "harness_runtime: null\n", "harness_runtime:\n  roles: []\n", ": : bad yaml ["):
            with self.subTest(text=text), self.assertRaises(slc.LaunchConfigError):
                slc.role_launch_overrides("mason", self._write(text))

    def test_unreadable_file_raises(self) -> None:
        with self.assertRaises(slc.LaunchConfigError):
            slc.role_launch_overrides("mason", Path(self._tmp.name) / "absent.yaml")

    def test_non_token_value_refused(self) -> None:
        # A declared value is spliced into argv: whitespace/shell metacharacters are a
        # config bug, never something to launch.
        for bad in ('"claude opus"', '"x; rm -rf ~"', '"--dangerously-skip-permissions"'):
            with self.subTest(bad=bad), self.assertRaises(slc.LaunchConfigError):
                slc.role_launch_overrides("mason", self._role(model=bad))

    def test_bracketed_model_suffix_accepted(self) -> None:
        cfg = slc.role_launch_overrides("mason", self._role(model='"claude-opus-5-5[1m]"'))
        self.assertEqual(cfg["model"], "claude-opus-5-5[1m]")


class CliTests(unittest.TestCase):
    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = slc.main(list(argv))
        return rc, out.getvalue()

    def test_cli_prints_two_key_lines(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "instance.yaml"
            p.write_text(_ROLE_YAML.format(model="claude-opus-5-5", effort_line=""), encoding="utf-8")
            rc, out = self._run("--persona", "mason", "--instance", str(p))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "model=claude-opus-5-5\neffort=\n")

    def test_cli_exit_2_on_unresolvable(self) -> None:
        rc, out = self._run("--persona", "mason", "--instance", "/nonexistent/instance.yaml")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_live_instance_yaml_seats_resolve(self) -> None:
        # The real config: both not-cut-over seats must resolve (T-4067 bumped them to 5-5).
        for persona in ("mason", "artisan"):
            with self.subTest(persona=persona):
                cfg = slc.role_launch_overrides(persona, slc._TOOLS.parent / "instance.yaml")
                self.assertTrue(cfg["model"], f"{persona} role declares no claude.model")


if __name__ == "__main__":
    unittest.main()
