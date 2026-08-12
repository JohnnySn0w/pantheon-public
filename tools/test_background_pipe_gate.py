#!/usr/bin/env python3
"""Tests for tools/hooks/background-pipe-gate.py (T-1907, T-0649 pipeline landing).

The hook filename is hyphenated (run as a script, not imported normally), so we
load it via importlib to unit-test its core decision functions — same pattern as
test_mcp_list_guard.py.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "background-pipe-gate.py"
_spec = importlib.util.spec_from_file_location("background_pipe_gate", _HOOK_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _bash(command: str, background: bool = True) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command, "run_in_background": background},
    }


class PipeMatchTest(unittest.TestCase):
    def test_matches_pipe_to_tail(self):
        self.assertTrue(gate.command_pipes_background_output("./smoke.sh 2>&1 | tail -20"))
        self.assertTrue(gate.command_pipes_background_output("cargo test | head -50"))
        self.assertTrue(gate.command_pipes_background_output("make check 2>&1 | grep FAIL"))

    def test_matches_stderr_pipe_and_path_invoked(self):
        self.assertTrue(gate.command_pipes_background_output("run.sh |& tail -5"))
        self.assertTrue(gate.command_pipes_background_output("run.sh | /usr/bin/tail -5"))

    def test_matches_filter_later_in_pipeline(self):
        self.assertTrue(gate.command_pipes_background_output("a | tee log | grep err"))

    def test_ignores_or_operator(self):
        self.assertFalse(gate.command_pipes_background_output("run.sh || tail -f fallback.log"))

    def test_ignores_non_filter_pipes(self):
        # tee is the canonical LEGITIMATE background pipe: it preserves the full
        # stream to a file. It must stay allowed however wide the filter set gets.
        self.assertFalse(gate.command_pipes_background_output("run.sh 2>&1 | tee out.log"))

    def test_t2328_blocks_the_wider_exit_code_eaters(self):
        """T-2328 (mnemosyne): the hazard belongs to the PIPE, not to the three
        filters the T-1686 anecdote happened to name. Absent pipefail, `$?` is
        the LAST command's — measured: `false | wc -l` rc=0, same for
        sed/awk/sort. And `| wc -l` is WORSE than `| tail` on the guard's own
        criterion: tail keeps the last N lines, wc keeps a single integer and
        discards the entire output. The gate blocked the milder
        evidence-destroyer and permitted the total one.

        NOTE this deliberately FLIPS a prior expectation: `cat a | sort | uniq -c`
        was asserted allowed in the original test. That assertion encoded the
        anecdote-scope, not a decision — T-1907 contains no mention of scope.
        """
        for cmd in (
            "cargo test 2>&1 | wc -l",
            "run.sh | sed -n 1p",
            "prog | awk '{print $2}'",
            "gen | cut -d: -f1",
            "cat a | sort | uniq -c",
            "curl -s url | jq .count",
            "false |& wc -c",
        ):
            with self.subTest(cmd=cmd):
                self.assertTrue(gate.command_pipes_background_output(cmd))

    def test_t2328_new_names_do_not_match_lookalikes(self):
        # Same boundary discipline as tailscale/grepdiff: a command that merely
        # STARTS with a filter name is not that filter.
        for cmd in ("a | awkward x", "b | sedge y", "c | wcurl z",
                    "d | sortof e", "e | jqp f", "f | cutter g", "g | uniqueize h"):
            with self.subTest(cmd=cmd):
                self.assertFalse(gate.command_pipes_background_output(cmd))

    def test_ignores_quoted_mentions(self):
        self.assertFalse(gate.command_pipes_background_output("echo 'never do cmd | tail -20'"))
        self.assertFalse(gate.command_pipes_background_output('git commit -m "fix: a | grep trap"'))

    def test_ignores_similar_command_names(self):
        # `tailscale` / `grepdiff`-style names must not match as the filter.
        self.assertFalse(gate.command_pipes_background_output("status | tailscale up"))
        self.assertFalse(gate.command_pipes_background_output("a | grepdiff x"))

    def test_ignores_plain_commands(self):
        self.assertFalse(gate.command_pipes_background_output("./smoke.sh > /tmp/out 2>&1"))
        self.assertFalse(gate.command_pipes_background_output(""))


class DecideTest(unittest.TestCase):
    def test_blocks_background_pipe(self):
        payload = gate.decide(_bash("./smoke.sh 2>&1 | tail -20"))
        self.assertIsNotNone(payload)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("READ time", out["permissionDecisionReason"])

    def test_allows_foreground_pipe(self):
        self.assertIsNone(gate.decide(_bash("./smoke.sh 2>&1 | tail -20", background=False)))

    def test_allows_background_without_run_flag(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "x | tail"}}
        self.assertIsNone(gate.decide(data))

    def test_allows_background_raw_redirect(self):
        self.assertIsNone(gate.decide(_bash("./smoke.sh > /tmp/out 2>&1")))

    def test_allows_non_bash_tools(self):
        data = {"tool_name": "Read", "tool_input": {"command": "x | tail"}}
        self.assertIsNone(gate.decide(data))

    def test_allows_empty_input(self):
        self.assertIsNone(gate.decide({}))


class EndToEndTest(unittest.TestCase):
    """Run the hook as a real subprocess, the way the harness invokes it."""

    def _run(self, stdin_text: str) -> tuple[int, str]:
        res = subprocess.run(
            [sys.executable, str(_HOOK_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return res.returncode, res.stdout

    def test_blocks_via_subprocess(self):
        rc, out = self._run(json.dumps(_bash("./smoke.sh 2>&1 | tail -20")))
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_allows_via_subprocess_silently(self):
        rc, out = self._run(json.dumps(_bash("./smoke.sh > /tmp/out 2>&1")))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_fail_open_on_garbage_stdin(self):
        rc, out = self._run("{not json")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_fail_open_on_empty_stdin(self):
        rc, out = self._run("")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
