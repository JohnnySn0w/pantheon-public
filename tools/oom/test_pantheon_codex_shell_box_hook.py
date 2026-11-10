#!/usr/bin/env python3
"""Tests for tools/oom/pantheon-codex-shell-box-hook.py (T-5480).

The hook is a codex PreToolUse command hook that rewrites a Bash tool command so it runs
through pantheon-shell-box.sh. Codex semantics (rust-v0.147.0 hooks/src/events/pre_tool_use.rs):
exit 0 + empty stdout = no rewrite; exit 2 + stderr BLOCKS the command; anything else is a
non-blocking failure. So every fail-open path here must be exit 0, empty stdout, empty stderr.
Every HOME is a temp dir.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_SRC = Path(__file__).resolve().parent / "pantheon-codex-shell-box-hook.py"
BOX_SRC = Path(__file__).resolve().parent / "pantheon-shell-box.sh"

TRICKY = [
    "echo 'single' \"double\" `x` $(echo sub)",
    "printf '%s\\n' a\nb\nc",
    "cat <<'EOF'\nhello $HOME 'q'\nEOF",
    "echo it's",
    "",
    "echo $((2+3)); exit 7",
    "echo 'ünï ✓' \\ back",
]


def event(command, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}


class HookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "oom"
        self.dir.mkdir()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.hook = self.dir / "pantheon-codex-shell-box-hook.py"
        shutil.copy(HOOK_SRC, self.hook)
        self.box = self.dir / "pantheon-shell-box.sh"
        shutil.copy(BOX_SRC, self.box)
        self.box.chmod(0o755)

    def run_hook(self, stdin, **kw):
        env = {**os.environ, "HOME": str(self.home)}
        return subprocess.run([sys.executable, "-I", str(self.hook)], input=stdin, text=True,
                              capture_output=True, timeout=30, env=env, **kw)

    def rewritten(self, command):
        r = self.run_hook(json.dumps(event(command)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "allow")
        return out["updatedInput"]["command"]

    def assert_fail_open(self, r):
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class RewriteTests(HookCase):
    def test_shape_and_quoting(self):
        for cmd in TRICKY:
            with self.subTest(cmd=cmd):
                new = self.rewritten(cmd)
                self.assertEqual(new, shlex.quote(str(self.box)) + " " + shlex.quote(cmd))
                self.assertEqual(shlex.split(new), [str(self.box), cmd])

    def test_idempotent(self):
        once = self.rewritten("echo hi")
        self.assert_fail_open(self.run_hook(json.dumps(event(once))))

    def test_end_to_end_matches_original(self):
        for cmd in TRICKY:
            with self.subTest(cmd=cmd):
                new = self.rewritten(cmd)
                env = {**os.environ, "PANTHEON_SHELL_BOX": "off", "HOME": str(self.home)}
                a = subprocess.run(["bash", "-lc", new], capture_output=True, text=True, env=env, timeout=30)
                b = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, env=env, timeout=30)
                # The box runs /bin/bash -c, so error text says "/bin/bash:" where the original
                # says "bash:" ($0 only); that is the one accepted cosmetic difference.
                self.assertEqual((a.returncode, a.stdout, a.stderr.replace("/bin/bash:", "bash:")),
                                 (b.returncode, b.stdout, b.stderr))


class FailOpenTests(HookCase):
    def test_bad_inputs_are_silent_exit_zero(self):
        cases = [
            "", "   ", "not json", "[]", "null", "42", "{",
            json.dumps({"tool_name": "Bash"}),
            json.dumps({"tool_name": "Bash", "tool_input": None}),
            json.dumps({"tool_name": "Bash", "tool_input": {}}),
            json.dumps({"tool_name": "Bash", "tool_input": {"command": 5}}),
            json.dumps({"tool_name": "Bash", "tool_input": {"command": ["ls"]}}),
            json.dumps({"tool_name": "Bash", "tool_input": "ls"}),
            json.dumps(event("ls", tool="apply_patch")),
            json.dumps(event("ls", tool="mcp__x__y")),
            json.dumps({"tool_name": ["Bash"], "tool_input": {"command": "ls"}}),
        ]
        for stdin in cases:
            with self.subTest(stdin=stdin):
                self.assert_fail_open(self.run_hook(stdin))

    def test_closed_stdin(self):
        r = subprocess.run([sys.executable, "-I", str(self.hook)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": str(self.home)})
        self.assert_fail_open(r)

    def test_missing_box_logs_and_fails_open(self):
        self.box.unlink()
        self.assert_fail_open(self.run_hook(json.dumps(event("ls"))))
        log = (self.home / ".local/state/pantheon/shell-box-fallback.log").read_text()
        self.assertEqual(len(log.strip().splitlines()), 1)
        self.assertIn("UNBOXED", log)

    def test_non_executable_box_logs_and_fails_open(self):
        self.box.chmod(0o644)
        self.assert_fail_open(self.run_hook(json.dumps(event("ls"))))
        self.assertTrue((self.home / ".local/state/pantheon/shell-box-fallback.log").exists())

    def test_unwritable_log_still_fails_open(self):
        self.box.unlink()
        (self.home / ".local").write_text("a file, so mkdir fails")
        self.assert_fail_open(self.run_hook(json.dumps(event("ls"))))

    def test_unset_home_still_fails_open(self):
        self.box.unlink()
        env = {k: v for k, v in os.environ.items() if k != "HOME"}
        r = subprocess.run([sys.executable, "-I", str(self.hook)], input=json.dumps(event("ls")),
                           text=True, capture_output=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
