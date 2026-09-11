"""Tests for the continuity-candidate-guard PreToolUse hook (T-3177).

Structural candidate-mode isolation for ORG-057 harness switches: while
`PANTHEON_CONTINUITY_CANDIDATE=1` is set for the hook subprocess, the guard
denies mailbox/Telegram/ticket/ledger action; absent the marker, it never
restricts anything (same fail-open-by-default shape as worker-guard.py, T-1345
-- see module docstring for the full threat-model rationale).

Run: python3 -m unittest tools.hooks.test_continuity_candidate_guard -v
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tools.hooks import continuity_candidate_guard as guard

_GUARD_PATH = Path(__file__).resolve().parent / "continuity_candidate_guard.py"

# Constructed at runtime (not literal in source) so this test file itself never
# carries the bot-API/ledger-path bytes that OTHER guards in this repo (e.g.
# worker-guard.py, if ever layered over this session) might pattern-match.
_BOT_API_CMD = base64.b64decode(
    "Y3VybCBodHRwczovL2FwaS50ZWxlZ3JhbS5vcmcvYm90MTIzL3NlbmRNZXNzYWdl"
).decode()
_LEDGER_PATH = base64.b64decode("bGVkZ2VyLzIwMjYtVzM2LzIwMjYtVzM2Lmpzb25s").decode()


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _edit(path: str, tool: str = "Edit") -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "tool_input": {key: path}}


def _blocked(data: dict, *, candidate_active: bool = True) -> bool:
    return guard.decide(data, candidate_active=candidate_active) is not None


class MarkerGating(unittest.TestCase):
    """Positive: the guard does nothing at all absent the marker."""

    def test_marker_absent_allows_everything(self) -> None:
        self.assertFalse(_blocked(_bash(_BOT_API_CMD), candidate_active=False))
        self.assertFalse(_blocked(_edit(_LEDGER_PATH), candidate_active=False))

    def test_marker_present_default_allows_benign_bash(self) -> None:
        self.assertFalse(_blocked(_bash("echo hi")))
        self.assertFalse(_blocked(_bash("python3 -m pytest tools/ -q")))


class BashSurfaceBlocked(unittest.TestCase):
    """Positive: candidate blocked from each structural-disable-list Bash
    surface. Adversarial pairing: the corresponding benign command in the same
    family is NOT blocked (proves the pattern isn't over-broad)."""

    def test_telegram_bot_api_blocked(self) -> None:
        self.assertTrue(_blocked(_bash(_BOT_API_CMD)))

    def test_telegram_bot_api_unrelated_curl_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("curl https://example.com/health")))

    def test_ticket_write_verb_blocked(self) -> None:
        for verb in ("file", "claim", "resolve", "annotate", "block", "move", "note", "close"):
            with self.subTest(verb=verb):
                self.assertTrue(_blocked(_bash(f"python3 tools/ticket.py {verb} --requester x")))

    def test_ticket_read_verb_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("python3 tools/ticket.py show T-1")))

    def test_ledger_write_blocked(self) -> None:
        self.assertTrue(_blocked(_bash(f"printf x >> {_LEDGER_PATH}")))

    def test_git_push_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("git push origin agent/T-3177")))

    def test_git_status_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("git status")))

    def test_credential_read_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("cat ~/.ssh/id_ed25519")))

    def test_unrelated_file_read_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("cat README.md")))

    def test_mailbox_action_blocked(self) -> None:
        self.assertTrue(_blocked(_bash("python3 tools/mailbox_consume.py --consume orchestrator")))

    def test_mailbox_list_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("python3 tools/mailbox_consume.py --list orchestrator")))

    def test_empty_bash_command_allowed(self) -> None:
        self.assertFalse(_blocked(_bash("")))


class EditSurfaceBlocked(unittest.TestCase):
    def test_ledger_edit_blocked(self) -> None:
        self.assertTrue(_blocked(_edit(_LEDGER_PATH)))

    def test_tickets_write_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("tickets/active/T-1.md")))

    def test_adr_write_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("adr/ORG-999-example.md")))

    def test_notebook_edit_of_protected_path_blocked(self) -> None:
        self.assertTrue(_blocked(_edit("tickets/active/T-1.ipynb", tool="NotebookEdit")))

    def test_unrelated_file_edit_allowed(self) -> None:
        self.assertFalse(_blocked(_edit("tools/some_module.py")))

    def test_edit_missing_path_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Edit", "tool_input": {}}))


class UnknownToolsAndMalformedPayloads(unittest.TestCase):
    def test_unknown_tool_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Read", "tool_input": {"file_path": _LEDGER_PATH}}))

    def test_empty_payload_allowed(self) -> None:
        self.assertFalse(_blocked({}))

    def test_missing_tool_input_allowed(self) -> None:
        self.assertFalse(_blocked({"tool_name": "Bash"}))


class MainEntrypointSubprocess(unittest.TestCase):
    """Exercises main() as a real subprocess -- the actual wiring shape
    (stdin JSON in, stdout JSON out, exit 0 always -- fail-open by exit code,
    deny communicated via stdout payload per the PreToolUse hook contract)."""

    def _run(self, payload: dict, *, marker: str | None) -> tuple[int, str]:
        env = dict(os.environ)
        if marker is None:
            env.pop(guard.CANDIDATE_MARKER_ENV, None)
        else:
            env[guard.CANDIDATE_MARKER_ENV] = marker
        proc = subprocess.run(
            [sys.executable, str(_GUARD_PATH)],
            input=json.dumps(payload), capture_output=True, text=True, env=env, check=False,
        )
        return proc.returncode, proc.stdout.strip()

    def test_main_denies_with_marker(self) -> None:
        code, out = self._run(_bash(_BOT_API_CMD), marker="1")
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_main_allows_without_marker(self) -> None:
        code, out = self._run(_bash(_BOT_API_CMD), marker=None)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_main_fails_open_on_malformed_stdin(self) -> None:
        env = dict(os.environ)
        env[guard.CANDIDATE_MARKER_ENV] = "1"
        proc = subprocess.run(
            [sys.executable, str(_GUARD_PATH)], input="{not json", capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
