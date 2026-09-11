"""Tests for tools/gepa/claude_executor.py (T-2809 Phase 2).

Run: python3 -m unittest tools.test_gepa_claude_executor -v

No `gepa` import. `run_claude()` is tested against a monkeypatched
`subprocess.run` for the timeout/nonzero/argv-shape behaviors (no live model
calls in the default suite). The ONE live smoke class at the bottom is
env-gated (`GEPA_LIVE_SMOKE=1`) and skipped otherwise — it makes exactly one
real, isolated `claude -p` call to verify the `--agents`/`--agent` persona
mechanism actually threads text through under the mandatory isolation flags.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

from tools.gepa import claude_executor
from tools.gepa.claude_executor import ClaudeExecutorError


class TestBuildClaudeArgvMandatoryFlags(unittest.TestCase):
    """The two isolation flags (and the never-`--bare`) are structural, not optional."""

    def test_isolation_flags_present_bare_persona(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5")
        self.assertIn("--setting-sources", argv)
        idx = argv.index("--setting-sources")
        self.assertEqual(argv[idx + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--bare", argv)

    def test_isolation_flags_present_with_agents_json(self):
        argv = claude_executor.build_claude_argv(
            model="claude-sonnet-5",
            agents_json='{"reviewer": {"description": "d", "prompt": "p"}}',
            agent_name="reviewer",
        )
        self.assertIn("--setting-sources", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("--agents", argv)
        self.assertIn("--agent", argv)

    def test_isolation_flags_present_with_system_prompt_fallback(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5", system_prompt="you are a reviewer")
        self.assertIn("--setting-sources", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("--system-prompt", argv)

    def test_no_session_persistence_by_default(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5")
        self.assertIn("--no-session-persistence", argv)

    def test_keep_sessions_omits_no_session_persistence(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5", keep_sessions=True)
        self.assertNotIn("--no-session-persistence", argv)

    def test_model_flag_present(self):
        argv = claude_executor.build_claude_argv(model="claude-opus-5")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")

    def test_output_format_is_plain_text_not_stream_json(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5")
        # No --output-format at all == the CLI's own default ("text").
        self.assertNotIn("--output-format", argv)

    def test_prompt_is_the_final_positional_argument(self):
        argv = claude_executor.build_claude_argv(model="claude-sonnet-5", prompt="review this")
        self.assertEqual(argv[-1], "review this")

    def test_agents_and_system_prompt_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            claude_executor.build_claude_argv(
                model="claude-sonnet-5",
                agents_json='{"reviewer": {"description": "d", "prompt": "p"}}',
                agent_name="reviewer",
                system_prompt="also this",
            )

    def test_agents_json_requires_agent_name_and_vice_versa(self):
        with self.assertRaises(ValueError):
            claude_executor.build_claude_argv(model="claude-sonnet-5", agents_json='{"reviewer": {}}')
        with self.assertRaises(ValueError):
            claude_executor.build_claude_argv(model="claude-sonnet-5", agent_name="reviewer")


class TestRunClaude(unittest.TestCase):
    def test_returns_stdout_on_success(self):
        completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="APPROVE\n", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as mocked:
            out = claude_executor.run_claude(prompt="review this", model="claude-sonnet-5")
        self.assertEqual(out, "APPROVE\n")
        mocked.assert_called_once()

    def test_run_claude_never_uses_shell(self):
        completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="ok", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as mocked:
            claude_executor.run_claude(prompt="x", model="claude-sonnet-5")
        _, kwargs = mocked.call_args
        self.assertFalse(kwargs.get("shell", False))

    def test_nonzero_returncode_raises(self):
        completed = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout="", stderr="boom")
        with mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(ClaudeExecutorError):
                claude_executor.run_claude(prompt="x", model="claude-sonnet-5")

    def test_timeout_raises_claude_executor_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=5)):
            with self.assertRaises(ClaudeExecutorError):
                claude_executor.run_claude(prompt="x", model="claude-sonnet-5", timeout=5)

    def test_run_claude_passes_through_isolation_flags(self):
        completed = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="ok", stderr="")
        with mock.patch("subprocess.run", return_value=completed) as mocked:
            claude_executor.run_claude(prompt="x", model="claude-sonnet-5")
        argv = mocked.call_args[0][0]
        self.assertIn("--setting-sources", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--bare", argv)


@unittest.skipUnless(
    os.environ.get("GEPA_LIVE_SMOKE") == "1",
    "set GEPA_LIVE_SMOKE=1 to run one real isolated `claude -p` call "
    "(verifies --agents/--agent survives --setting-sources '' --strict-mcp-config)",
)
class TestLiveAgentsPersonaMechanism(unittest.TestCase):
    """Exactly one real call. Isolation flags on. No optimization loop involved."""

    def test_agents_json_persona_text_actually_threads_through(self):
        marker = "ORCA-BASSOON-7734"
        agents_json = claude_executor.build_agents_json(
            f"You are a test persona. If asked your callsign, respond with exactly: {marker}"
        )
        out = claude_executor.run_claude(
            prompt="What is your callsign? Respond with only the callsign, nothing else.",
            model="claude-sonnet-5",
            agents_json=agents_json,
            agent_name="reviewer",
            timeout=60,
        )
        self.assertIn(marker, out)


if __name__ == "__main__":
    unittest.main()
