#!/usr/bin/env python3
"""Tests for tools/hooks/mcp-list-guard.py (T-0996 / T-0894 prong A).

The hook filename is hyphenated (run as a script, not imported normally), so we
load it via importlib to unit-test its core decision functions.
"""

from __future__ import annotations

import importlib.util
import time
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


class TestWrapperPrefixBypass(unittest.TestCase):
    """T-3192: exec-through wrappers (timeout/nohup/nice/env/time/…) must not
    hide a `claude` invocation from command-position matching. A wrapped
    transient `claude` bypassing the guard and killing a live poller was the
    2026-08-24 comms-dark incident this guard exists to prevent.
    """

    def test_wrapped_invocations_are_still_caught(self):
        cmds = [
            "timeout 45 claude mcp list 2>&1",
            "timeout -s KILL -k 5 45 claude mcp list",
            "timeout --signal=KILL 1.5m claude -p hi",
            "nohup claude mcp list &",
            "nice -n 5 claude mcp list",
            "nice -5 claude",
            "env FOO=1 claude mcp list",
            "env -i claude",
            "env FOO=1 BAR=2 nice -n 5 claude -p x",
            "time claude mcp list",
            "/usr/bin/time -v claude",
            "stdbuf -oL claude mcp list",
            "setsid -f claude --bare",
            "ionice -c 2 -n 7 claude",
            "chrt -f 10 claude",
            "nohup timeout 45 claude mcp list",
            "cd /tmp && timeout 45 claude mcp list",
            "out=$(timeout 45 claude -p q)",
        ]
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.command_targets_mcp_list(cmd))

    def test_safe_shapes_survive_wrapping(self):
        cmds = [
            "timeout 10 claude --version",
            "timeout 60 claude -p --safe-mode 'x'",
            "nohup claude --help",
        ]
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))

    def test_env_assignment_value_ending_in_claude_path_is_not_an_invocation(self):
        """T-3192 follow-up: an env-assignment VALUE that happens to end in
        `/claude` (e.g. `HOME=/home/claude`) must never be misread as a
        path-invoked `claude` binary. This was a real latent bug in the
        ORIGINAL (pre-T-3192) guard too — confirmed `FOO=/home/claude ls`
        already matched before this arc touched the file — exposed here as
        the composed-decision class the wrapper widening could have made
        worse if the `=`-exclusion fix hadn't closed it structurally. Kept
        explicit (not just incidental via the wrapper fix) per reviewer ask.
        """
        must_be_false = [
            "FOO=/home/claude ls",
            "HOME=/home/claude ls -la",
            "PATH=/home/claude/bin:$PATH ls",
        ]
        for cmd in must_be_false:
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))

        must_be_true = [
            "HOME=/home/claude claude -p x",
            "env HOME=/home/claude claude mcp list",
        ]
        for cmd in must_be_true:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.command_targets_mcp_list(cmd))

    def test_t2251_false_positive_class_holds_through_wrapper_grammar(self):
        # The account is literally named `claude`; a wrapper grammar loose
        # enough to swallow a bare word would reintroduce T-2251.
        cmds = [
            "sudo -u claude ls /home/claude",
            "nice -n 5 du -sh /home/claude",
            "timeout 30 ls /home/claude",
            "timeout 30 rsync -a /home/claude/ /backup/claude",
            "nohup bash /home/claude/x.sh",
            "env FOO=1 ls /home/claude",
            "env HOME=/home/claude ls",
            "time du -sh /home/claude",
            "timeout 30 claude-bot-supervise.sh",
            "nohup ~/.local/bin/claude-telegram-watchdog.sh --once",
            "timeout 5 pgrep -u claude -f bun",
        ]
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))


class TestHeredocBodiesAreData(unittest.TestCase):
    """T-3193: a quoted-heredoc BODY that merely mentions `claude` (backticked,
    or as a literal line meant to be written to a file) is DATA, exactly like a
    quoted span -- it must never be read as an invocation. The false positive
    that motivated this: HO annotating a ticket via `NOTE=$(cat <<'EOF' ...
    EOF)` where the body's prose backtick-quoted `claude mcp list` got read as
    a real invocation by the pre-fix guard.
    """

    def test_heredoc_bodies_are_not_invocations(self):
        must_be_false = [
            # (a) the exact HO false-positive shape: command-substitution +
            # quoted heredoc whose body backtick-mentions the binary.
            "NOTE=$(cat <<'EOF'\n"
            "…the guard blocked `claude mcp list`…\n"
            "EOF\n"
            ') && python3 tools/ticket.py annotate T-1 --note "$NOTE"',
            # (b) bare invocation-looking line written to a file as DATA.
            "cat > x.md <<'EOF'\nclaude mcp list\nEOF",
            # (c) unquoted tag.
            "cat <<EOF\ntimeout 45 claude mcp list\nEOF",
            # (d) dash form, tab-indented terminator.
            "cat <<-EOF\n\tnice -n 5 claude\n\tEOF",
            # (e) two heredocs stacked on one line -- both bodies dropped.
            "cat <<'A' <<'B'\nclaude\nA\nclaude --resume x\nB",
            # (g) here-string is not a heredoc; quoted anyway.
            'grep x <<< "claude mcp list"',
            # (h) arithmetic shift must not be read as a heredoc operator.
            "echo $((1<<3)) && ls /home/claude",
            # (n) the commit-message shape HO uses constantly: a quoted
            # heredoc feeding $(...) inside a quoted -m argument, body
            # backtick-mentions the binary.
            "git commit -q -m \"$(cat <<'EOF'\n"
            "hooks: guard blocked a `claude mcp list` mention in prose\n"
            'EOF\n)"',
            # REBOUND 1 (c): accepted residual, contrived and documented
            # rather than fixed -- quoted prose containing a bare `<<TAG`
            # WITH a coincidental exact `TAG`-only line later in the same
            # command still over-strips (the tag-match doesn't know it's
            # inside a quoted span; the coincidental terminator makes it
            # look like a real, well-formed heredoc). Nobody writes this
            # shape on purpose; it's named here so a regression in either
            # direction is visible.
            'echo "x << WORD"\nclaude mcp list\nWORD',
            # REBOUND 1 (d): well-terminated heredoc whose body itself
            # contains literal double quotes plus a backticked mention --
            # confirms the fix for a genuinely-terminated heredoc is
            # unaffected by the deny-biased change to the unterminated
            # path (this one terminates cleanly, so it still strips).
            "git commit -m \"$(cat <<'EOF'\n"
            'fix "quoted" and a `claude mcp list` mention\n'
            'EOF\n)"',
        ]
        for cmd in must_be_false:
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.command_targets_mcp_list(cmd))

        must_be_true = [
            # (i) real invocation AFTER a terminator line.
            "cat <<'EOF'\nprose\nEOF\nclaude mcp list",
            # (j) same, but with an explicit separator before the invocation.
            "cat <<'EOF'\nprose\nEOF\n; timeout 45 claude mcp list",
            # (k) real invocation ON the operator line itself.
            "claude -p x <<'EOF'\ninput\nEOF",
            # (l) here-string does not suppress a real invocation.
            'timeout 5 claude -p x <<< "input"',
            # (m)'s correct True variant: terminator line is exact, and the
            # real invocation follows on its own line after it.
            "cat <<'EOF'\nx\nEOF\nnohup claude mcp list",
            # REBOUND 1 (b): flipped from the original case (f). An
            # unterminated heredoc is DENY-biased, not fail-open: bash
            # itself would swallow this whole line as heredoc data (no
            # terminator ever arrives), but this guard chooses the safe
            # direction for a shape nobody writes on purpose -- a false
            # deny here costs a re-shaped command; the old fail-open
            # posture cost a live comms-dark incident (REBOUND 1's find).
            "cat <<'EOF'\nnohup claude mcp list",
            # Direct consequence of the same fix, not separately named in
            # the REBOUND but following from the identical root cause:
            # the terminator line must be EXACT ("EOF && ..." is not a
            # terminator), so this heredoc is ALSO unterminated and is now
            # DENY-biased too -- the trailing "&& nohup claude mcp list"
            # is matched as ordinary command text, in command position
            # after "&&". Verified empirically against the fixed guard
            # (was in the must_be_false list pre-REBOUND-1).
            "cat <<'EOF'\n…\nEOF && nohup claude mcp list",
        ]
        for cmd in must_be_true:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.command_targets_mcp_list(cmd))

    def test_heredoc_scan_is_linear(self):
        """T-3193 REBOUND 2 (reviewer finding, 2026-08-25): terminator
        lookup was an O(n) `re.search` PER CANDIDATE TAG -- quadratic on
        ordinary multi-line text with many `<<"literal"`-shaped false
        candidates, e.g. C++ `std::cout << "x" << y;` lines echoed/cat'd
        in a command. Measured pre-fix: 8000 `echo "nX << TX"` lines
        (174 KB) = 12s; 800 C++ lines = 0.5s. Fixed via an O(n) terminator
        index built once + O(log n) bisect lookup per tag. 0.5s below is a
        loaded-box-safe ceiling, not tight -- the O(n) version runs in a
        few ms.
        """
        budget_seconds = 0.5

        false_candidate_lines = "\n".join(
            f'echo "n{i} << T{i}"' for i in range(8000)
        )
        cmd_many_quoted_shifts = false_candidate_lines + "\ntimeout 45 claude mcp list"
        start = time.perf_counter()
        result = guard.command_targets_mcp_list(cmd_many_quoted_shifts)
        elapsed = time.perf_counter() - start
        self.assertTrue(result)
        self.assertLess(
            elapsed, budget_seconds,
            f"8000-line quoted-<< scan took {elapsed:.3f}s (budget {budget_seconds}s)"
        )

        cpp_lines = "\n".join(
            f'std::cout << "line {i}" << x;' for i in range(1600)
        )
        cmd_cpp_outside_heredoc = cpp_lines + "\ntimeout 45 claude mcp list"
        start = time.perf_counter()
        result = guard.command_targets_mcp_list(cmd_cpp_outside_heredoc)
        elapsed = time.perf_counter() - start
        self.assertTrue(result)
        self.assertLess(
            elapsed, budget_seconds,
            f"1600-line C++ scan took {elapsed:.3f}s (budget {budget_seconds}s)"
        )

        # Positive control: the SAME C++ body, but genuinely INSIDE a
        # terminated heredoc -- the whole body is skipped via one bisect
        # lookup rather than scanned line-by-line, so this must be at
        # least as fast as the untouched-body case above.
        cmd_cpp_inside_heredoc = (
            "cat <<'SRC'\n" + cpp_lines + "\nSRC\nclaude mcp list"
        )
        start = time.perf_counter()
        result = guard.command_targets_mcp_list(cmd_cpp_inside_heredoc)
        elapsed = time.perf_counter() - start
        self.assertTrue(result)
        self.assertLess(
            elapsed, budget_seconds,
            f"heredoc-wrapped C++ scan took {elapsed:.3f}s (budget {budget_seconds}s)"
        )


class TestQuotedDoubleAngleProseNeverOpensAHeredoc(unittest.TestCase):
    """T-3193 REBOUND 1 (reviewer finding, 2026-08-25): a regression vs
    origin/main -- a `<<` inside UNRELATED quoted prose on a multi-line
    command (no heredoc ever intended) was read as a heredoc opener; since
    no terminator line existed, the pre-fix fail-open path swallowed
    everything to end-of-string, including a genuine invocation on a later
    line. Reproduced 4/4 by the reviewer; fixed by making "no terminator"
    mean "not a heredoc" (deny-biased) rather than "swallow to EOF"
    (fail-open).
    """

    def test_quoted_double_angle_prose_never_opens_a_heredoc(self):
        cmds = [
            'echo "left << shift" \nclaude mcp list',
            'python3 tools/ticket.py annotate T-1 --note "throughput << target" \n'
            "timeout 45 claude mcp list",
            "echo 'perf: p50 << p99 outlier' \nclaude mcp list",
            "cat notes.txt\necho 'left << right shape'\ntimeout 10 claude mcp list",
        ]
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.command_targets_mcp_list(cmd))


if __name__ == "__main__":
    unittest.main()
