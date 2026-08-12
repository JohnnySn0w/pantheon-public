#!/usr/bin/env python3
"""Tests for tools/hooks/mcp-list-guard.py (T-0996 / T-0894 prong A).

The hook filename is hyphenated (run as a script, not imported normally), so we
load it via importlib to unit-test its core decision functions.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "mcp-list-guard.py"
_spec = importlib.util.spec_from_file_location("mcp_list_guard", _HOOK_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class CommandMatchTest(unittest.TestCase):
    def test_matches_claude_mcp_list(self):
        self.assertTrue(guard.command_targets_mcp_list("claude mcp list"))
        self.assertTrue(guard.command_targets_mcp_list("claude mcp list --json"))
        self.assertTrue(guard.command_targets_mcp_list("cd /tmp && claude mcp list 2>&1"))

    def test_matches_any_second_claude_invocation(self):
        # T-1623 widening: ANY plugin-initializing claude, not just mcp list.
        self.assertTrue(guard.command_targets_mcp_list("claude"))
        self.assertTrue(guard.command_targets_mcp_list("claude --bare"))
        self.assertTrue(guard.command_targets_mcp_list("claude --resume abc123"))
        self.assertTrue(guard.command_targets_mcp_list("/usr/local/bin/claude --continue"))
        self.assertTrue(guard.command_targets_mcp_list("cd /tmp && claude"))
        # --print WITHOUT --safe-mode still initializes plugins → dangerous.
        self.assertTrue(guard.command_targets_mcp_list("claude --print 'do things'"))
        self.assertTrue(guard.command_targets_mcp_list("FOO=bar claude -p hello"))

    def test_safe_shapes_exempted(self):
        self.assertFalse(guard.command_targets_mcp_list("claude --version"))
        self.assertFalse(guard.command_targets_mcp_list("claude --help"))
        self.assertFalse(guard.command_targets_mcp_list("claude -h"))
        # The veneration resurrect design: headless + safe-mode = plugin-less.
        self.assertFalse(guard.command_targets_mcp_list("claude --print --safe-mode 'x'"))
        self.assertFalse(guard.command_targets_mcp_list("claude -p --safe-mode 'x'"))

    def test_quoted_mentions_are_not_invocations(self):
        # T-1623's own false positive: the guard fired on a ticket BODY that
        # mentioned the subcommand inside a quoted --ask argument.
        self.assertFalse(guard.command_targets_mcp_list(
            'python3 tools/ticket.py file --ask "never run claude mcp list on a live bot"'
        ))
        self.assertFalse(guard.command_targets_mcp_list(
            "echo 'claude --resume is dangerous' >> notes.md"
        ))

    def test_arguments_named_claude_are_not_invocations(self):
        # `claude` as a username/argument is not command position.
        self.assertFalse(guard.command_targets_mcp_list("sudo -u claude ls /home/claude"))
        self.assertFalse(guard.command_targets_mcp_list("pgrep -u claude -f bun"))
        self.assertFalse(guard.command_targets_mcp_list("journalctl --user -u claude-telegram.service"))

    def test_claude_prefixed_scripts_are_not_claude(self):
        # Commands merely PREFIXED with claude- are different binaries.
        self.assertFalse(guard.command_targets_mcp_list("claude-bot-supervise.sh"))
        self.assertFalse(guard.command_targets_mcp_list("~/.local/bin/claude-telegram-watchdog.sh --once"))

    def test_does_not_match_unrelated(self):
        self.assertFalse(guard.command_targets_mcp_list("ps aux | grep bun"))
        self.assertFalse(guard.command_targets_mcp_list("git mcp list"))  # no 'claude'
        self.assertFalse(guard.command_targets_mcp_list("curl .../getMe"))
        self.assertFalse(guard.command_targets_mcp_list(""))


class DecideTest(unittest.TestCase):
    def _bash(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def test_blocks_mcp_list_when_poller_live(self):
        payload = guard.decide(self._bash("claude mcp list"), poller_live=True)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("409", payload["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_mcp_list_when_poller_not_live(self):
        # No live poller → nothing to kill → allow (the diagnostic IS the use case).
        self.assertIsNone(guard.decide(self._bash("claude mcp list"), poller_live=False))

    def test_allows_non_mcp_command_even_with_live_poller(self):
        self.assertIsNone(guard.decide(self._bash("claude --version"), poller_live=True))

    def test_allows_non_bash_tool(self):
        data = {"tool_name": "Read", "tool_input": {"command": "claude mcp list"}}
        self.assertIsNone(guard.decide(data, poller_live=True))

    def test_allows_empty_input(self):
        self.assertIsNone(guard.decide({}, poller_live=True))


class TestCommandSubstitutionFalsePositive(unittest.TestCase):
    """T-2251: `$(cd /home/claude && …)` was denied as a `claude` invocation.

    Two independent defects had to line up, and the fix needs both halves — each
    test below is paired against the half it exercises, so reverting either one
    fails this class rather than passing quietly:

      1. `$(`, `)` and backtick were not segment boundaries. `)` in particular
         meant `FOO=$(date) claude -p x` had no command position before `claude`.
      2. the env-assignment VALUE was `\\S*`, which swallowed `$(cd ` (and then
         the `;` the normalisation inserts) as one value — leaving the PATH token
         `/home/claude` parsed as the command.

    The account is literally named `claude`, so `$(cd /home/claude && …)` is an
    everyday shape on the most-used path on the box. A safety guard that
    false-positives on ordinary commands is one that gets routed around, and a
    routed-around guard protects nothing.
    """

    # Reported by bisection on T-2251; command substitution AROUND a cd whose
    # target ends in /claude. Either element alone was already fine.
    def test_allows_substitution_around_cd_into_claude_home(self):
        for cmd in (
            "out=$(cd /home/claude && echo '{}' | bash .claude/hooks/handoff-auto-read.sh 2>&1)",
            "out=$(cd /home/claude && echo '{}' | bash x.sh 2>&1)",
            "sz=$(cd /home/claude && stat -c %s HANDOFF.md)",
            "d=$(cd /home/claude/pantheon-config && git rev-parse HEAD)",
            "n=$(cd /home/claude-fallback && wc -l < HANDOFF.md)",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))

    def test_still_blocks_real_invocation_after_a_substitution(self):
        """Guards defect 1. A false ALLOW here reintroduces the 409 bridge-death,
        so this is the case that must never regress: the `)` has to close a
        segment or `claude` is not seen to be in command position at all."""
        self.assertTrue(guard.command_targets_mcp_list("FOO=$(date) claude -p hi"))

    def test_still_blocks_claude_as_the_substitution_command(self):
        for cmd in ("out=$(claude -p q)", "out=$(cd /tmp && claude -p q)", "`claude -p q`"):
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.command_targets_mcp_list(cmd))

    def test_still_blocks_env_assigned_invocation(self):
        """Guards defect 2's fix from over-tightening: a legitimate `VAR=value
        claude …` prefix must still parse as an env assignment and block."""
        self.assertTrue(guard.command_targets_mcp_list("FOO=bar claude -p x"))

    def test_path_mention_is_never_an_invocation(self):
        """The general property behind T-2251: a path COMPONENT named claude is
        not a command, in or out of a substitution."""
        for cmd in (
            "cd /home/claude && bash x.sh",
            "ls /home/claude/pantheon-config",
            "rsync -a /home/claude/ /backup/claude/",
        ):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))


if __name__ == "__main__":
    unittest.main()
