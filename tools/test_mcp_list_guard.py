#!/usr/bin/env python3
"""Tests for tools/hooks/mcp-list-guard.py (T-0996 / T-0894 prong A).

The hook filename is hyphenated (run as a script, not imported normally), so we
load it via importlib to unit-test its core decision functions.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import random
import re
import signal
import statistics
import time
import unittest
from pathlib import Path

_HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "mcp-list-guard.py"
_spec = importlib.util.spec_from_file_location("mcp_list_guard", _HOOK_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _doubling_ratio(run_n, run_2n, repeats=9, clock=time.thread_time):
    """Time `run_n()` then `run_2n()` back-to-back, `repeats` times, on `clock`.

    T-4230: the default clock is CPU time of the calling thread, NOT wall time. A
    loaded host steals wall clock unpredictably (measured over 30 runs at 17x
    contention, the wall-clock median-of-9 ratio ranged 1.46..4.02 against a 3.0
    bound; the CPU-time one ranged 2.01..2.30). CPU time is not advanced while the
    scan is preempted, and this scan is pure-Python compute, so it is the honest
    measure of the algorithm's work.

    Returns (median of the per-repeat t(2N)/t(N) ratios, best t(2N) seen). Measuring
    N and 2N microseconds apart cancels a slowly-varying ambient load level; the
    median discards the minority of repeats where a stall landed inside one scan but
    not the other.
    """
    ratios = []
    best_2n = float("inf")
    for _ in range(repeats):
        start = clock()
        run_n()
        t_n = clock() - start
        start = clock()
        run_2n()
        t_2n = clock() - start
        ratios.append(t_2n / t_n)
        best_2n = min(best_2n, t_2n)
    return statistics.median(ratios), best_2n


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
        index built once + O(log n) bisect lookup per tag.

        T-4118: the assertion is a SCALING RATIO, not a wall-clock budget.
        The old `assertLess(elapsed, 0.5s)` false-failed under `pytest -n
        auto` contention (the scan itself runs in tens of ms; a loaded box
        just stole the wall clock). Complexity, not wall time, is what this
        test exists to guard, so we time the scan at N and 2N false
        candidates and assert the DOUBLING RATIO stays below a linear bound.
        A linear (O(n log n)) scan gives t(2N)/t(N) ~ 2; the pre-fix
        quadratic per-tag lookup gives ~4.

        The ratio is made load-insensitive by measuring N and 2N
        BACK-TO-BACK inside each repeat and taking the MEDIAN of the
        per-repeat ratios. A per-repeat ratio cancels the ambient load
        level -- both scans were slowed by the same factor microseconds
        apart -- and the median discards the minority of repeats where a
        preemption landed inside one scan but not the other (in either
        direction). Min-of-ratios was rejected: it is linear-safe but
        ALSO suppresses the quadratic signal (under load-16 a real
        quadratic variant's min-ratio fell to 2.05, below the bound), so
        it hides the regression this test exists to catch. Measured on a
        load-14 box, median-of-9-ratios: linear impl 1.87-2.25, quadratic
        variant 3.47-3.92 -- 3.0 separates them with ~0.75 margin either
        side. The absolute ceiling below is kept ONLY as a hang guard,
        deliberately generous, never the primary signal; a
        deliberately-reintroduced quadratic terminator lookup fails BOTH
        the ratio and the ceiling (verified under T-4118).

        T-4230: the median-of-9 WALL-clock ratio still flaked on a heavily
        loaded box (over 30 runs at 17x CPU contention it ranged 1.46..4.02,
        crossing the 3.0 bound). The measurement is now CPU time
        (`time.thread_time`) via `_doubling_ratio`, which a preemption cannot
        inflate: 2.01..2.30 over the same 30 runs. Same bound, same median-of-9
        back-to-back design; the quadratic variant is re-verified to fail it
        (see DoublingRatioHarnessT4230 for the permanent harness self-check).
        """
        # `echo "n{i} << T{i}"` = one `<<"literal"`-shaped FALSE heredoc
        # candidate per line -> one terminator lookup per line, which is
        # exactly the per-candidate work the quadratic bug multiplied by n.
        def build(n_false_candidates):
            lines = "\n".join(
                f'echo "n{i} << T{i}"' for i in range(n_false_candidates)
            )
            return lines + "\ntimeout 45 claude mcp list"

        n = 3000
        cmd_n = build(n)
        cmd_2n = build(2 * n)

        def scan(cmd):
            # Positive control: the invocation after the false candidates is
            # still seen at both sizes -- the scan is doing real work, not
            # short-circuiting to a trivially-fast constant.
            def run():
                self.assertTrue(guard.command_targets_mcp_list(cmd))
            return run

        median_ratio, best_2n = _doubling_ratio(scan(cmd_n), scan(cmd_2n))

        # PRIMARY signal: doubling the input at most ~doubles the work.
        self.assertLess(
            median_ratio, 3.0,
            f"heredoc terminator scan looks super-linear: median "
            f"t(2N)/t(N)={median_ratio:.2f} (CPU time, 9 repeats) -- a "
            f"quadratic per-candidate terminator lookup lands near 4.",
        )

        # SECONDARY hang guard ONLY (never the primary signal), in CPU seconds so
        # host load cannot trip it: even the best-case largest scan finishes far
        # under a ceiling. The linear scan costs tens of ms of CPU; a truly
        # quadratic one blows past this on a corpus this size (measured ~7.8-9.7s),
        # so the ceiling independently catches the regression too.
        self.assertLess(
            best_2n, 5.0,
            f"largest heredoc scan took {best_2n:.2f}s CPU -- over the 5s hang ceiling",
        )

        # Positive control (correctness, not timing): the SAME kind of
        # false-candidate body, but genuinely INSIDE a terminated heredoc,
        # is skipped wholesale via one bisect lookup, and the invocation
        # after the terminator is still found.
        cpp_lines = "\n".join(
            f'std::cout << "line {i}" << x;' for i in range(1600)
        )
        cmd_cpp_inside_heredoc = (
            "cat <<'SRC'\n" + cpp_lines + "\nSRC\nclaude mcp list"
        )
        self.assertTrue(guard.command_targets_mcp_list(cmd_cpp_inside_heredoc))


def _burn(iterations):
    """Pure-Python CPU work whose cost is proportional to `iterations`."""
    x = 0
    for i in range(iterations):
        x += i
    return x


class DoublingRatioHarnessT4230(unittest.TestCase):
    """T-4230: proves the doubling-ratio measurement itself, independent of the hook,
    so the linearity test cannot be silently neutered by a later edit to the harness.

    The linearity test's bound (3.0) is only meaningful if (a) the harness really
    separates linear from quadratic work and (b) it is immune to a host stall that
    lands inside one scan and not the other -- the wall-clock harness was not, which
    is what made the linearity test flake on a loaded box (T-4230)."""

    def test_separates_linear_from_quadratic_work(self):
        linear, _ = _doubling_ratio(lambda: _burn(100_000), lambda: _burn(200_000))
        quadratic, _ = _doubling_ratio(lambda: _burn(100_000), lambda: _burn(400_000))
        self.assertLess(linear, 3.0, f"linear work measured {linear:.2f}")
        self.assertGreater(quadratic, 3.0, f"quadratic work measured {quadratic:.2f}")

    def test_a_stall_outside_the_cpu_does_not_move_the_ratio(self):
        # Stand-in for the host preempting the scan (17x contention): every 2N run
        # also sleeps 150ms of WALL time and burns no extra CPU. A wall-clock
        # harness reads ~30 here; a CPU-time harness must still read ~2.
        def stalled_2n():
            _burn(200_000)
            time.sleep(0.15)

        ratio, _ = _doubling_ratio(lambda: _burn(100_000), stalled_2n, repeats=5)
        self.assertLess(ratio, 3.0, f"harness measured wall time, not CPU time: {ratio:.2f}")


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


# T-4392 phase 2b: the pre-fix command-position regex, kept VERBATIM as the
# differential oracle. The hook now computes the same match sequence with a
# linear scanner (`_claude_invocation_args`); this regex is super-linear
# (quadratic on newline runs, exponential on `timeout -1 -1 ...`), so it is
# only ever run on the short inputs below.
_OLD_WRAPPER_ARG = r"(?:-\S*|-?\d+(?:\.\d+)?[smhd]?|[A-Z][A-Z0-9_]*)"
_OLD_WRAPPER = (
    r"(?:[^\s=]*/)?"
    r"(?:command|exec|nohup|setsid|timeout|time|nice|env|stdbuf|ionice|chrt)"
    r"(?:\s+" + _OLD_WRAPPER_ARG + r")*"
)
_OLD_ENV_ASSIGN = r"[A-Za-z_][A-Za-z0-9_]*=[^\s;&|`\n()]*"
_OLD_CLAUDE_CMD_RE = re.compile(
    r"(?:^|[;&|`\n(])\s*"
    r"(?:(?:" + _OLD_WRAPPER + r"|" + _OLD_ENV_ASSIGN + r")\s+)*"
    r"(?:[^\s=]*/)?claude(?=\s|[;&|`)\n]|$)"
    r"([^;&|`\n]*)"
)


def _old_args(stripped):
    return [m.group(1) or "" for m in _OLD_CLAUDE_CMD_RE.finditer(stripped)]


def _old_verdict(command):
    """command_targets_mcp_list as it was before T-4392, on the oracle regex."""
    if not command:
        return False
    stripped = guard._SUBST_RE.sub(
        ";", guard._QUOTED_RE.sub(" ", guard._strip_heredoc_bodies(command)))
    for args in _old_args(stripped):
        if guard._SAFE_ARGS_RE.search(args):
            continue
        if guard._PRINT_RE.search(args) and guard._SAFE_MODE_RE.search(args):
            continue
        return True
    return False


def _existing_test_commands():
    """Every string literal in this file that looks like a shell command -- the
    corpus the existing behavioural tests pin."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
            out.add(node.value)
    return sorted(out)


_FUZZ_WORDS = (
    "claude", "/usr/bin/claude", "claude-bot-supervise.sh", "a/claude", "x/claude;",
    "claude;x", "(claude", "a;claude", "/home/claude", "HOME=/home/claude", "FOO=a;b",
    "command", "exec", "nohup", "setsid", "timeout", "time", "nice", "env", "stdbuf",
    "ionice", "chrt", "/usr/bin/env", "-x/env", "-x/claude", "a=b/env", "envx",
    "A=1", "a=1", "_=", "X=/claude", "--print", "-p", "--safe-mode", "--version",
    "--help", "-h", "-n", "5", "-5", "1.5m", "KILL", "FOO", "ls", "mcp", "list", "-",
)
_FUZZ_SEPS = (";", "&", "|", "`", "\n", "(", ")", " ", "  ", "\t", " ; ", " && ",
              "\n ", " ", "\x1c", "$(")


def _fuzz_commands(seed=4392, count=4000):
    rng = random.Random(seed)
    for _ in range(count):
        parts = []
        for _ in range(rng.randint(1, 10)):
            parts.append(rng.choice(_FUZZ_WORDS))
            parts.append(rng.choice(_FUZZ_SEPS) if rng.random() < 0.35 else " ")
        if rng.random() < 0.5:
            parts.pop()
        yield "".join(parts)


class TestLinearScannerMatchesOldRegexT4392(unittest.TestCase):
    """T-4392 phase 2b: the linear scanner must compute EXACTLY what the old
    regex computed -- the same (args) sequence, hence the same verdict -- on
    every input. Differential against the verbatim old regex above."""

    def _assert_same(self, cmd):
        stripped = guard._SUBST_RE.sub(
            ";", guard._QUOTED_RE.sub(" ", guard._strip_heredoc_bodies(cmd)))
        self.assertEqual(list(guard._claude_invocation_args(stripped)),
                         _old_args(stripped), repr(cmd))
        # raw (un-normalised) text too: the scanner mirrors the regex on ANY string
        self.assertEqual(list(guard._claude_invocation_args(cmd)), _old_args(cmd), repr(cmd))
        self.assertEqual(guard.command_targets_mcp_list(cmd), _old_verdict(cmd), repr(cmd))

    def test_existing_corpus(self):
        corpus = _existing_test_commands()
        self.assertGreater(len(corpus), 100)
        for cmd in corpus:
            if len(cmd) <= 400:  # the oracle is slow on the big linearity inputs
                self._assert_same(cmd)

    def test_seeded_fuzz(self):
        start = time.thread_time()
        n = 0
        denies = 0
        for cmd in _fuzz_commands():
            self._assert_same(cmd)
            n += 1
            denies += guard.command_targets_mcp_list(cmd)
        self.assertEqual(n, 4000)
        # the corpus must exercise both verdicts, not just one side
        self.assertGreater(denies, 400)
        self.assertLess(n - denies, n - 400)
        self.assertLess(time.thread_time() - start, 5.0)


_LINEAR_BOUND_S = 1.0
_HARD_STOP_S = 10
# The doubling-ratio test runs 3 repeats at two sizes (~0.5s CPU in all) under ONE
# hard stop; on a heavily contended CI runner that is several seconds of wall clock,
# so it gets more headroom. The stop only turns a hang into a failure, and the CPU-time
# ratio and bound still decide pass/fail.
_RATIO_HARD_STOP_S = 30


@contextlib.contextmanager
def _hard_stop(label, seconds=_HARD_STOP_S):
    """SIGALRM wall-clock hard stop: a super-linear regression FAILS instead of
    hanging the suite (the regex engine checks signals, so this interrupts it)."""
    def _hang(signum, frame):  # pragma: no cover - only on a regression
        raise AssertionError(f"hung past {seconds}s wall clock: {label!r}")

    old = signal.signal(signal.SIGALRM, _hang)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _cpu_bounded(test, fn, *args, label=""):
    """fn(*args), asserting under _LINEAR_BOUND_S of CPU time (time.thread_time,
    load-insensitive), under the hard stop (tools/test_worker_guard.py style)."""
    with _hard_stop(label):
        start = time.thread_time()
        result = fn(*args)
        elapsed = time.thread_time() - start
    test.assertLess(elapsed, _LINEAR_BOUND_S, f"{label!r} took {elapsed:.3f}s CPU")
    return result


class TestCommandPositionScanIsLinearT4392(unittest.TestCase):
    """T-4392 phase 2b: this hook runs on EVERY Bash call on the seats, so its
    cost must be linear in the command length. Pre-fix, `"\\n"*N` cost 3.3s CPU at
    4 KB (quadratic: each newline a fresh command position rescanning the rest)
    and `"timeout " + "-1 "*22` (74 bytes) cost 6.4s (exponential: `-1` matches
    two wrapper-arg alternatives). Each shape below runs at 64 KiB well under
    1s CPU, and doubling N at most ~doubles the work."""

    SIZE = 64 * 1024
    # A non-invoking `claude` mention: the scanner short-circuits on text with no
    # `claude` at all, so the False shapes carry one to force the full scan.
    MENTION = " ls /home/claude"

    # (prefix, unit, suffix, expected verdict at every size)
    SHAPES = (
        ("", "\n", MENTION, False),
        ("", "a=1\n", MENTION, False),
        ("", "; ", MENTION, False),
        ("", "env ", "claude", True),
        ("", "A=1 ", MENTION, False),
        ("", "A=1 ", "claude", True),
        ("", "\n ", MENTION, False),
        ("", ";a", MENTION, False),
        ("", "(/", MENTION, False),
        ("timeout ", "-1 ", MENTION, False),        # exponential pre-fix
        ("timeout ", "-x/timeout ", MENTION, False),
        ("", "timeout -1 ", "claude", True),
    )

    @staticmethod
    def _build(prefix, unit, suffix, size):
        return prefix + unit * (size // len(unit)) + suffix

    def test_each_shape_is_fast_at_64k(self):
        for prefix, unit, suffix, verdict in self.SHAPES:
            cmd = self._build(prefix, unit, suffix, self.SIZE)
            with self.subTest(unit=unit, suffix=suffix):
                got = _cpu_bounded(self, guard.command_targets_mcp_list, cmd,
                                   label=f"{prefix!r}+{unit!r}*N+{suffix!r}")
                self.assertEqual(got, verdict)

    def test_doubling_ratio_is_linear(self):
        for prefix, unit, suffix, _ in self.SHAPES:
            cmd_n = self._build(prefix, unit, suffix, self.SIZE // 2)
            cmd_2n = self._build(prefix, unit, suffix, self.SIZE)
            with self.subTest(unit=unit, suffix=suffix):
                with _hard_stop(unit, _RATIO_HARD_STOP_S):
                    ratio, best_2n = _doubling_ratio(
                        lambda: guard.command_targets_mcp_list(cmd_n),
                        lambda: guard.command_targets_mcp_list(cmd_2n),
                        repeats=3,
                    )
                self.assertLess(ratio, 3.0, f"{unit!r}: t(2N)/t(N)={ratio:.2f}")
                self.assertLess(best_2n, _LINEAR_BOUND_S)


if __name__ == "__main__":
    unittest.main()
