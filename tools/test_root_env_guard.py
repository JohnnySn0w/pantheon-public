#!/usr/bin/env python3
"""Tests for tools/hooks/root-env-guard.py and tools/pantheon-sudo (T-5331)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
_HOOK_PATH = _TOOLS / "hooks" / "root-env-guard.py"
_spec = importlib.util.spec_from_file_location("root_env_guard", _HOOK_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PW = "root" + ".env"  # the file name, assembled so this test file stays greppable-safe


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class BashDecisionTest(unittest.TestCase):
    def assertDenied(self, command: str) -> None:
        verdict = guard.decide(_bash(command))
        self.assertIsNotNone(verdict, command)
        self.assertEqual(verdict["hookSpecificOutput"]["permissionDecision"], "deny")

    def assertAllowed(self, command: str) -> None:
        self.assertIsNone(guard.decide(_bash(command)), command)

    def test_any_mention_of_the_password_file_is_denied(self):
        for cmd in (
            f"cat /home/claude/{PW}",
            f"sed -n 1p ~/{PW}",
            f"tr -d '\\n' < /home/claude/{PW} | sudo -S -k -p '' true",
            f"grep -r {PW} tools/",
            f"python3 -c \"print(open('/home/claude/{PW}').read())\"",
            f"cp ~/{PW} /tmp/x",
            f"less ~/ROOT.ENV",
        ):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_sudo_stdin_is_denied_in_every_spelling(self):
        for cmd in (
            "echo pw | sudo -S true",
            "sudo -kS -p '' systemctl restart x",
            "sudo -Su codex id",
            "sudo --stdin true",
            "cd /tmp && sudo -n -S ls",
            "(sudo -S ls)",
        ):
            with self.subTest(cmd=cmd):
                self.assertDenied(cmd)

    def test_wrapper_and_ordinary_commands_pass(self):
        for cmd in (
            "/home/claude/pantheon-config/tools/pantheon-sudo systemctl restart earlyoom",
            "/home/claude/pantheon-config/tools/pantheon-sudo -u codex bash -c 'git status'",
            "sudo -n -u codex true",
            "sudo systemctl status foo",
            "grep -r 'root[.]env' tools/",
            "ls -la /home/claude",
            "git log --oneline -3",
            "pytest -S tools/",  # -S belongs to pytest, not sudo
        ):
            with self.subTest(cmd=cmd):
                self.assertAllowed(cmd)


class FileToolDecisionTest(unittest.TestCase):
    def test_file_tools_naming_the_file_are_denied(self):
        cases = [
            {"tool_name": "Read", "tool_input": {"file_path": f"/home/claude/{PW}"}},
            {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": f"/home/claude/{PW}"}},
            {"tool_name": "Glob", "tool_input": {"pattern": f"**/{PW}"}},
            {"tool_name": "Edit", "tool_input": {"file_path": f"/home/claude/{PW}",
                                                 "old_string": "a", "new_string": "b"}},
            {"tool_name": "Write", "tool_input": {"file_path": f"/tmp/{PW}", "content": "x"}},
        ]
        for data in cases:
            with self.subTest(tool=data["tool_name"]):
                self.assertIsNotNone(guard.decide(data))

    def test_file_tools_elsewhere_pass(self):
        self.assertIsNone(guard.decide({"tool_name": "Read",
                                        "tool_input": {"file_path": "/home/claude/HANDOFF.md"}}))
        # content that only MENTIONS the file name stays editable (docs, the hook itself)
        self.assertIsNone(guard.decide({"tool_name": "Edit", "tool_input": {
            "file_path": "/home/claude/notes.md", "old_string": "a", "new_string": f"see {PW}"}}))
        self.assertIsNone(guard.decide({"tool_name": "Grep", "tool_input": {
            "pattern": PW, "path": "/home/claude/pantheon-config/tools"}}))
        self.assertIsNone(guard.decide({"tool_name": "WebFetch",
                                        "tool_input": {"url": f"https://x/{PW}"}}))


class HookProcessTest(unittest.TestCase):
    def _run(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(_HOOK_PATH)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)

    def test_deny_and_allow_through_the_real_entrypoint(self):
        denied = self._run(_bash(f"cat ~/{PW}"))
        self.assertIn('"deny"', denied.stdout)
        allowed = self._run(_bash("ls"))
        self.assertEqual(allowed.returncode, 0)
        self.assertNotIn('"deny"', allowed.stdout)

    def test_unparseable_payload_fails_closed(self):
        proc = subprocess.run([sys.executable, str(_HOOK_PATH)], input="not json",
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


class WrapperTest(unittest.TestCase):
    """pantheon-sudo, with a fake `sudo` first on PATH that records argv and stdin."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = Path(self._tmp.name)
        self.pw = d / "pw"
        self.pw.write_text("s3cret\n")
        self.log = d / "log"
        fake = d / "sudo"
        fake.write_text(f"#!/bin/bash\nprintf '%s\\n' \"$@\" > {self.log}\ncat >> {self.log}\n")
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", PANTHEON_SUDO_PW_FILE=str(self.pw))

    def tearDown(self):
        self._tmp.cleanup()

    def test_pipes_the_password_without_newline_and_passes_args(self):
        proc = subprocess.run([str(_TOOLS / "pantheon-sudo"), "-u", "codex", "id"],
                              env=self.env, capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")  # nothing of the password on stdout
        self.assertEqual(self.log.read_text(), "-S\n-k\n-p\n\n-u\ncodex\nid\ns3cret")

    def test_usage_and_unreadable_file(self):
        proc = subprocess.run([str(_TOOLS / "pantheon-sudo")], env=self.env,
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 64)
        env = dict(self.env, PANTHEON_SUDO_PW_FILE=str(self.pw) + ".missing")
        proc = subprocess.run([str(_TOOLS / "pantheon-sudo"), "true"], env=env,
                              capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 65)
        self.assertNotIn("s3cret", proc.stderr)

    def test_piped_stdin_is_refused_loudly_and_sudo_never_runs(self):
        # T-5713 (svc:vote 10-27): `echo x | pantheon-sudo tee -a FILE` exited 0 and wrote
        # nothing, because the command's stdin is the (already drained) password pipe.
        # Passing the caller's stdin through is unsafe: a NOPASSWD/cached command never
        # reads the password line, so the PASSWORD would flow into the command (tee -> file).
        proc = subprocess.run([str(_TOOLS / "pantheon-sudo"), "tee", "-a", "/tmp/x"], env=self.env,
                              input="payload\n", capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 66, proc.stderr)
        self.assertIn("stdin", proc.stderr)
        self.assertNotIn("s3cret", proc.stderr + proc.stdout)
        self.assertFalse(self.log.exists(), "sudo must not run when stdin is refused")

    def test_file_redirect_stdin_is_refused(self):
        src = Path(self._tmp.name) / "in.txt"
        src.write_text("payload\n")
        with open(src) as fh:
            proc = subprocess.run([str(_TOOLS / "pantheon-sudo"), "tee", "-a", "/tmp/x"], env=self.env,
                                  stdin=fh, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 66, proc.stderr)
        self.assertFalse(self.log.exists())

    def test_dev_null_stdin_still_runs(self):
        proc = subprocess.run([str(_TOOLS / "pantheon-sudo"), "true"], env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.log.exists())


if __name__ == "__main__":
    unittest.main()
