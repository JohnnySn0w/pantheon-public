#!/usr/bin/env python3
"""Tests for tools/hooks/background-pipe-gate.py (T-1907, T-0649 pipeline landing).

The hook filename is hyphenated (run as a script, not imported normally), so we
load it via importlib to unit-test its core decision functions — same pattern as
test_mcp_list_guard.py.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import json
import random
import re
import signal
import statistics
import subprocess
import sys
import time
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
        """T-2328 (archivist): the hazard belongs to the PIPE, not to the three
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

    def test_fail_closed_on_garbage_stdin(self):
        # T-4392 phase 2c DELIBERATE FLIP: was test_fail_open_on_garbage_stdin (rc 0).
        # An unparseable payload is ambiguity; the fail-closed runner denies it.
        res = subprocess.run([sys.executable, str(_HOOK_PATH)], input="{not json",
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(res.stdout.strip(), "")
        self.assertIn("BLOCKED (background-pipe-gate, fail-closed, T-4392): ambiguity: ",
                      res.stderr)

    def test_empty_stdin_allows(self):
        # T-4392 phase 2c: renamed from test_fail_open_on_empty_stdin. The outcome is
        # unchanged (the runner reads an empty payload as {}), but it is no longer fail-open.
        rc, out = self._run("")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")


# T-4392 phase 2c: the pre-fix matcher, kept VERBATIM as the differential oracle. The
# hook now computes the same truth value with a linear scanner (`_pipes_into_filter`);
# this regex is quadratic (`'|/'*n`: 2.7s CPU at 16 KB), so it only runs on short inputs.
_OLD_PIPE_FILTER_RE = re.compile(
    r"(?<!\|)\|&?(?!\|)"                    # | or |& but not ||
    r"\s*(?:command\s+|exec\s+)?"           # common wrappers
    r"(?:\S*/)?(?:head|tail|grep|wc|sed|awk|cut|sort|uniq|jq)(?=\s|$|[;&|`)\n])"
)


def _old_verdict(command):
    """command_pipes_background_output as it was before T-4392 phase 2c."""
    if not command:
        return False
    return bool(_OLD_PIPE_FILTER_RE.search(gate._QUOTED_RE.sub(" ", command)))


def _existing_test_commands():
    """Every non-empty string literal in this file: the corpus the behavioural tests pin."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    return sorted({node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value})


_FUZZ_WORDS = (
    "|", "||", "|&", "|&|", "&", "/", "//", "a/", "/usr/bin/", "./", "x", "run.sh", "2>&1",
    "command", "exec", "command ", "exec ", "commandx", "execute",
    "head", "tail", "grep", "wc", "sed", "awk", "cut", "sort", "uniq", "jq",
    "tailscale", "grepdiff", "awkward", "wcurl", "jqp", "sortof", "uniqueize", "tee",
    ";", "`", ")", "(", "$", "-20", "-n", "'", '"', "'a | tail'", '"b | grep"',
)
_FUZZ_HOT = ("|", "|&", "| ", "|/", "x/", "tail", "grep", "wc", "jq", "command ", "exec ")
_FUZZ_SEPS = (" ", "  ", "\t", "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1f", "\x85", " ",
              " ", "　", "​", "")


def _fuzz_commands(seed=4392, count=6000):
    rng = random.Random(seed)
    for _ in range(count):
        parts = []
        for _ in range(rng.randint(1, 12)):
            # bias toward pipes and filter names so both verdicts are well represented
            pool = _FUZZ_HOT if rng.random() < 0.4 else _FUZZ_WORDS
            parts.append(rng.choice(pool))
            parts.append(rng.choice(_FUZZ_SEPS) if rng.random() < 0.6 else "")
        yield "".join(parts)


class TestLinearScannerMatchesOldRegexT4392(unittest.TestCase):
    """T-4392 phase 2c: the linear scanner must give EXACTLY the old regex's verdict on
    every input (quoted-stripped AND raw text), differential against the verbatim oracle."""

    def _assert_same(self, cmd):
        stripped = gate._QUOTED_RE.sub(" ", cmd)
        self.assertEqual(gate._pipes_into_filter(stripped),
                         bool(_OLD_PIPE_FILTER_RE.search(stripped)), repr(cmd))
        self.assertEqual(gate._pipes_into_filter(cmd),
                         bool(_OLD_PIPE_FILTER_RE.search(cmd)), repr(cmd))
        self.assertEqual(gate.command_pipes_background_output(cmd), _old_verdict(cmd), repr(cmd))

    def test_existing_corpus(self):
        corpus = [c for c in _existing_test_commands() if len(c) <= 400]
        self.assertGreater(len(corpus), 60)
        for cmd in corpus:
            self._assert_same(cmd)

    def test_seeded_fuzz(self):
        start = time.thread_time()
        n = denies = 0
        for cmd in _fuzz_commands():
            self._assert_same(cmd)
            n += 1
            denies += _old_verdict(cmd)
        self.assertEqual(n, 6000)
        # the corpus must exercise both verdicts, not just one side
        self.assertGreater(denies, 600)
        self.assertGreater(n - denies, 600)
        self.assertLess(time.thread_time() - start, 10.0)

    def test_whitespace_class_is_the_regex_engines(self):
        # Every code point the engine calls \s must separate exactly as the oracle says.
        for cp in range(0x110000):
            ch = chr(cp)
            if not re.match(r"\s", ch):
                continue
            for cmd in (f"a |{ch}tail", f"a |{ch}x/tail", f"a |x{ch}/tail", f"a |tail{ch}",
                        f"a |command{ch}tail", f"a |{ch}exec{ch}/bin/wc"):
                self._assert_same(cmd)


def _doubling_ratio(run_n, run_2n, repeats=9, clock=time.thread_time):
    """Median t(2N)/t(N) on CPU time, N and 2N back to back (tools/test_mcp_list_guard.py,
    T-4230: CPU time, so host load cannot move the ratio). Returns (median, best t(2N))."""
    ratios = []
    best_2n = float("inf")
    for _ in range(repeats):
        start = clock()
        run_n()
        t_n = clock() - start
        start = clock()
        run_2n()
        t_2n = clock() - start
        ratios.append(t_2n / max(t_n, 1e-9))
        best_2n = min(best_2n, t_2n)
    return statistics.median(ratios), best_2n


_LINEAR_BOUND_S = 1.0
_HARD_STOP_S = 10
_RATIO_HARD_STOP_S = 30


@contextlib.contextmanager
def _hard_stop(label, seconds=_HARD_STOP_S):
    """SIGALRM wall-clock hard stop: a super-linear regression FAILS instead of hanging
    the suite (the regex engine checks signals, so this interrupts it)."""
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
    with _hard_stop(label):
        start = time.thread_time()
        result = fn(*args)
        elapsed = time.thread_time() - start
    test.assertLess(elapsed, _LINEAR_BOUND_S, f"{label!r} took {elapsed:.3f}s CPU")
    return result


class TestPipeScanIsLinearT4392(unittest.TestCase):
    """T-4392 phase 2c: pre-fix, `'|/'*n` cost 0.15s CPU at 4 KB, 0.70s at 8 KB and
    2.7s at 16 KB (quadratic). Every shape below now runs at 64 KiB well under 1s CPU,
    and doubling N at most ~doubles the work."""

    SIZE = 64 * 1024

    # (prefix, unit, suffix, expected verdict at every size)
    SHAPES = (
        ("", "|/", "", False),                 # the measured quadratic shape
        ("", "|x", "", False),
        ("", "|/", "|tail", True),
        ("", "|/", "x/tail", True),
        ("", "|&/", "", False),
        ("", "| command /", "", False),
        ("", "| exec x/", "", False),
        ("", "| ", "", False),
        ("", "| ", "", False),
        ("", "/|", "", False),
        ("", "a/b", "|x/", False),
        ("a |", "x", "/tail", True),           # one pipe, one long run, the name at the end
        ("", "'", "", False),                  # quote shapes (the _QUOTED_RE pass)
        ("", "'\"", "", False),
        ("'", "a", "", False),
        ("", "\"x|/", "", False),
    )

    @staticmethod
    def _build(prefix, unit, suffix, size):
        return prefix + unit * (size // len(unit)) + suffix

    def test_each_shape_is_fast_at_64k(self):
        for prefix, unit, suffix, verdict in self.SHAPES:
            cmd = self._build(prefix, unit, suffix, self.SIZE)
            with self.subTest(unit=unit, suffix=suffix):
                got = _cpu_bounded(self, gate.command_pipes_background_output, cmd,
                                   label=f"{prefix!r}+{unit!r}*N+{suffix!r}")
                self.assertEqual(got, verdict)

    def test_doubling_ratio_is_linear(self):
        for prefix, unit, suffix, _ in self.SHAPES:
            cmd_n = self._build(prefix, unit, suffix, self.SIZE // 2)
            cmd_2n = self._build(prefix, unit, suffix, self.SIZE)
            with self.subTest(unit=unit, suffix=suffix):
                with _hard_stop(unit, _RATIO_HARD_STOP_S):
                    ratio, best_2n = _doubling_ratio(
                        lambda: gate.command_pipes_background_output(cmd_n),
                        lambda: gate.command_pipes_background_output(cmd_2n),
                        # 9, not 3: a median of 3 flaked to 3.09 in CI (#1215) and to 3.08
                        # in 1 of 5 local trials; 5 still reached 2.88. 9 kept every one of
                        # 5 trials <= 2.3, clear of 3.0 and of the ~4 a quadratic gives.
                        repeats=9,
                    )
                self.assertLess(ratio, 3.0, f"{unit!r}: t(2N)/t(N)={ratio:.2f}")
                self.assertLess(best_2n, _LINEAR_BOUND_S)


if __name__ == "__main__":
    unittest.main()
