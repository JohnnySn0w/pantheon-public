#!/usr/bin/env python3
"""Hermetic tests for the urgent-mail-inject PreToolUse hook (T-1641 Ph2)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "urgent-mail-inject.py"
_spec = importlib.util.spec_from_file_location("urgent_mail_inject", _HOOK)
umi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(umi)

_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import mailbox  # noqa: E402


class ConstantsInSyncTest(unittest.TestCase):
    def test_env_var_name_matches_mailbox_source_of_truth(self):
        # The hook hardcodes a copy (documented, hot-path reason) — this test is
        # the guard against the two silently drifting apart.
        self.assertEqual(umi._URGENT_PHASE2_ENV, mailbox._URGENT_PHASE2_ENV)


class ResolveInboxTest(unittest.TestCase):
    def test_override_env_wins(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get(umi._INBOX_OVERRIDE_ENV)
            os.environ[umi._INBOX_OVERRIDE_ENV] = d
            try:
                self.assertEqual(umi._resolve_inbox(), Path(d))
            finally:
                if old is None:
                    os.environ.pop(umi._INBOX_OVERRIDE_ENV, None)
                else:
                    os.environ[umi._INBOX_OVERRIDE_ENV] = old

    def test_unknown_user_returns_none(self):
        old = os.environ.pop(umi._INBOX_OVERRIDE_ENV, None)
        try:
            import unittest.mock as mock
            with mock.patch("getpass.getuser", return_value="totally-unknown-user"):
                self.assertIsNone(umi._resolve_inbox())
        finally:
            if old is not None:
                os.environ[umi._INBOX_OVERRIDE_ENV] = old


class BuildNudgeTest(unittest.TestCase):
    def test_names_sender_and_subject(self):
        text = umi.build_nudge("orchestrator", "disk 91%")
        self.assertIn("orchestrator", text)
        self.assertIn("disk 91%", text)
        self.assertIn("drain now", text)


class TestEndToEndSubprocess(unittest.TestCase):
    """Drive the hook as a real subprocess (the actual stdin/stdout contract)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        self.inbox.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _env(self, *, flag_on: bool) -> dict:
        env = {
            "URGENT_MAIL_INBOX": str(self.inbox),
            "PATH": "/usr/bin:/bin",
        }
        if flag_on:
            env["PANTHEON_MAILBOX_URGENT_PHASE2_ENABLED"] = "1"
        if "LD_LIBRARY_PATH" in os.environ:
            env["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH"]
        return env

    def _run(self, payload: dict, *, flag_on: bool) -> tuple[int, str]:
        p = subprocess.run(
            [sys.executable, str(_HOOK)],
            input=json.dumps(payload), capture_output=True, text=True,
            env=self._env(flag_on=flag_on),
        )
        return p.returncode, p.stdout

    def _write_marker(self, sender="orchestrator", subject="wedge") -> None:
        (self.inbox / umi._MARKER_NAME).write_text(
            json.dumps({"sender": sender, "subject": subject, "msgid": "x", "ts": 1.0})
        )

    def test_flag_off_no_op_even_with_marker_present(self) -> None:
        self._write_marker()
        rc, out = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                            flag_on=False)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
        self.assertTrue((self.inbox / umi._MARKER_NAME).exists(),
                        "flag-off must not even touch the marker")

    def test_flag_on_no_marker_no_op(self) -> None:
        rc, out = self._run({"tool_name": "Read", "tool_input": {}}, flag_on=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_flag_on_marker_present_injects_and_clears(self) -> None:
        self._write_marker(sender="recovery", subject="disk 91% WARN")
        rc, out = self._run({"tool_name": "Read", "tool_input": {}}, flag_on=True)
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        hso = obj["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", hso)  # T-4650: context only, no approval
        self.assertIn("recovery", hso["additionalContext"])
        self.assertIn("disk 91% WARN", hso["additionalContext"])
        self.assertFalse((self.inbox / umi._MARKER_NAME).exists(),
                         "marker must be cleared on injection")

    def test_second_call_after_clear_is_silent(self) -> None:
        self._write_marker()
        rc1, out1 = self._run({"tool_name": "Read", "tool_input": {}}, flag_on=True)
        self.assertNotEqual(out1.strip(), "")
        rc2, out2 = self._run({"tool_name": "Edit", "tool_input": {}}, flag_on=True)
        self.assertEqual(rc2, 0)
        self.assertEqual(out2.strip(), "", "exactly one nudge per urgent send")

    def test_corrupt_marker_fails_open_and_clears(self) -> None:
        (self.inbox / umi._MARKER_NAME).write_text("not json at all")
        rc, out = self._run({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                            flag_on=True)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")
        self.assertFalse((self.inbox / umi._MARKER_NAME).exists(),
                         "a corrupt marker must not wedge every future call")

    def test_malformed_stdin_still_fails_open(self) -> None:
        self._write_marker()
        p = subprocess.run(
            [sys.executable, str(_HOOK)],
            input="{not json", capture_output=True, text=True,
            env=self._env(flag_on=True),
        )
        # This hook does not depend on stdin content at all, so malformed stdin
        # must not suppress a real pending urgent nudge.
        self.assertEqual(p.returncode, 0)
        self.assertNotEqual(p.stdout.strip(), "")

    # NOTE: "unresolvable inbox" (no override + unmapped unix user) is
    # deliberately NOT exercised as a subprocess end-to-end case here — the
    # implementer/CI unix user is `claude`, which maps to a REAL persona
    # (orchestrator) whose REAL mailbox hub may exist on this host, so dropping the
    # override would make this test read production filesystem state. That
    # branch is covered hermetically at the import level instead: see
    # ResolveInboxTest.test_unknown_user_returns_none above.


if __name__ == "__main__":
    unittest.main()
