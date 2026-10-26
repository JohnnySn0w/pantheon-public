#!/usr/bin/env python3
"""background-pipe-gate wrapped by tools/hooks/_failclosed.py (T-4392 phase 2c).

  * CI CANARY: an ordinary-command corpus must stay ALL-ALLOW through the wrap, so a
    throw-on-everything or deny-on-everything bug cannot merge. The corpus is the checked-in
    tools/fixtures/failclosed_canary_bash.json run as FOREGROUND calls (the gate's rule never
    fires there, so any deny is a wrap bug), plus background-specific ordinary commands run
    with run_in_background: true. Every fixture command run in the BACKGROUND must get the
    same verdict as the pre-2c oracle regex (the differential, through the wrap).
  * IMPORT FAILURE of _failclosed denies (exit 2), including a broken helper; a decoy helper
    on PYTHONPATH cannot win.
  * The real script end to end: the rule deny is unchanged; the 262,144-character cap denies;
    the worst scanner shape at the cap stays well inside the budget.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TOOLS = Path(__file__).resolve().parent
_HOOKS = _TOOLS / "hooks"
_HOOK = _HOOKS / "background-pipe-gate.py"
_CORPUS = _TOOLS / "fixtures" / "failclosed_canary_bash.json"

sys.path.insert(0, str(_HOOKS))
import _failclosed as fc  # noqa: E402

_spec = importlib.util.spec_from_file_location("background_pipe_gate_fc", _HOOK)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

# The pre-2c matcher, verbatim (the same oracle as tools/test_background_pipe_gate.py).
_OLD_PIPE_FILTER_RE = re.compile(
    r"(?<!\|)\|&?(?!\|)"                    # | or |& but not ||
    r"\s*(?:command\s+|exec\s+)?"           # common wrappers
    r"(?:\S*/)?(?:head|tail|grep|wc|sed|awk|cut|sort|uniq|jq)(?=\s|$|[;&|`)\n])"
)

# Ordinary BACKGROUND commands the seats run every day: all must ALLOW (no filter pipe).
BACKGROUND_ORDINARY = (
    "sleep 60",
    "sleep 300; echo done",
    "timeout 900 taskset -c 3 python3 -m pytest -q tools/test_failclosed.py > /tmp/log 2>&1",
    "python3 -m pytest -q tools/ > /tmp/pytest.log 2>&1; echo rc=$?",
    "cargo test --workspace > /tmp/cargo.log 2>&1",
    "./smoke.sh 2>&1 | tee /tmp/smoke.log",
    "make check > out.txt 2>&1 || echo FAILED",
    "run.sh || tail -f fallback.log",
    "tail -f /var/log/syslog > /tmp/sys.log",
    "grep -rn TODO tools/ > /tmp/todo.txt",
    "npm run build > build.log 2>&1 && npm test > test.log 2>&1",
    "git fetch origin && git log --oneline -5 > /tmp/log.txt",
    "python3 tools/long_job.py --out /tmp/out.json",
    "bash -c 'set -o pipefail; long | tail -5'",
    'echo "never pipe to | tail in the background" > /tmp/note',
    "journalctl -u foo --since today > /tmp/j.txt",
    "status | tailscale up",
    "find . -name '*.py' -newer x > /tmp/changed",
    "watch -n 5 date > /dev/null",
    "nohup ./server --port 8080 > server.log 2>&1 &",
)


def _payload(command: str, background: bool) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command, "run_in_background": background}}


def _stdin(command: str, background: bool) -> str:
    return json.dumps(_payload(command, background))


def _fixture() -> list[str]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))["commands"]


def _old_verdict(command: str) -> bool:
    return bool(command) and bool(_OLD_PIPE_FILTER_RE.search(gate._QUOTED_RE.sub(" ", command)))


def _evaluate(command: str, background: bool):
    with mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0):
        return fc.evaluate("background-pipe-gate", gate.decide,
                           max_command_chars=gate.MAX_COMMAND_CHARS,
                           stdin=io.StringIO(_stdin(command, background)))


def _run_script(stdin_text: str, hook: Path = _HOOK, env: dict | None = None):
    if env is None:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, str(hook)], input=stdin_text, capture_output=True,
                          text=True, env=env, timeout=60)


class CanaryAllAllow(unittest.TestCase):
    def test_corpus_is_substantial(self):
        self.assertGreaterEqual(len(_fixture()), 100)
        self.assertGreaterEqual(len(BACKGROUND_ORDINARY), 20)

    def test_fixture_foreground_all_allow_in_process(self):
        for command in _fixture():
            with self.subTest(command=command[:80]):
                code, out, err, _ = _evaluate(command, background=False)
                self.assertEqual((code, out, err), (0, "", ""))

    def test_background_ordinary_all_allow_in_process(self):
        for command in BACKGROUND_ORDINARY:
            with self.subTest(command=command):
                self.assertFalse(_old_verdict(command), "canary entry is not ordinary")
                code, out, err, _ = _evaluate(command, background=True)
                self.assertEqual((code, out, err), (0, "", ""))

    def test_fixture_background_matches_the_oracle_through_the_wrap(self):
        denies = 0
        for command in _fixture():
            with self.subTest(command=command[:80]):
                code, out, err, _ = _evaluate(command, background=True)
                self.assertEqual((code, err), (0, ""))
                if _old_verdict(command):
                    denies += 1
                    self.assertEqual(
                        json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
                else:
                    self.assertEqual(out, "")
        self.assertGreater(denies, 0)  # the fixture has foreground filter pipes: a real signal

    def test_real_script_sample_allows(self):
        fixture = _fixture()
        sample = [(c, False) for c in fixture[:: max(1, len(fixture) // 20)]]
        sample += [(c, True) for c in BACKGROUND_ORDINARY]
        for command, background in sample:
            with self.subTest(command=command[:80], background=background):
                r = _run_script(_stdin(command, background))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_real_script_rule_deny_is_unchanged(self):
        r = _run_script(_stdin("./smoke.sh 2>&1 | tail -20", True))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("READ time", out["permissionDecisionReason"])
        # the same command in the foreground is untouched
        r = _run_script(_stdin("./smoke.sh 2>&1 | tail -20", False))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_non_bash_and_empty_payloads_allow(self):
        for text in ("", json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}),
                     json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})):
            with self.subTest(text=text):
                r = _run_script(text)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class AmbiguityDenies(unittest.TestCase):
    def test_garbage_and_non_object_payloads_deny(self):
        for text in ("{not json", "[1, 2]", json.dumps({"tool_name": "Bash", "tool_input": "x"}),
                     json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}})):
            with self.subTest(text=text):
                r = _run_script(text)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertIn("BLOCKED (background-pipe-gate, fail-closed, T-4392): ambiguity: ",
                              r.stderr)

    def test_exception_in_decide_denies(self):
        def boom(data):
            raise RuntimeError("synthetic")
        with mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0):
            code, out, err, _ = fc.evaluate("background-pipe-gate", boom,
                                            stdin=io.StringIO(_stdin("ls", True)))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("exception: RuntimeError: synthetic", err)


class ImportFailureDenies(unittest.TestCase):
    def _copy_hook(self, td: Path) -> Path:
        dst = td / "background-pipe-gate.py"
        shutil.copyfile(_HOOK, dst)
        return dst

    def _run(self, hook: Path):
        return _run_script(_stdin("ls", True), hook=hook)

    def test_missing_helper_denies_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(self._copy_hook(Path(td)))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (background-pipe-gate, fail-closed, T-4392): cannot import "
                      "_failclosed", r.stderr)
        self.assertIn("The hook's directory lacks _failclosed.py: run the hook from the "
                      "pantheon-config clone, or unwire it via the Edit tool.", r.stderr)
        self.assertNotIn("failed to import", r.stderr)

    def test_broken_helper_denies(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("raise RuntimeError('corrupt helper')\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("corrupt helper", r.stderr)
        self.assertIn("the hook's _failclosed.py failed to import (RuntimeError): fix it, or "
                      "unwire the hook via the Edit tool.", r.stderr)
        self.assertNotIn("lacks _failclosed.py", r.stderr)

    def test_syntax_error_helper_is_broken_not_missing(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("def (:\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("failed to import (SyntaxError)", r.stderr)

    def test_helper_missing_its_own_dependency_is_broken_not_missing(self):
        with tempfile.TemporaryDirectory() as td:
            hook = self._copy_hook(Path(td))
            (Path(td) / "_failclosed.py").write_text("import no_such_module_t4392\n")
            r = self._run(hook)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("failed to import (ModuleNotFoundError)", r.stderr)

    def test_decoy_on_pythonpath_loses(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as decoy:
            hook = self._copy_hook(Path(td))
            shutil.copyfile(_HOOKS / "_failclosed.py", Path(td) / "_failclosed.py")
            (Path(decoy) / "_failclosed.py").write_text(
                "import sys\ndef run(*a, **k):\n    sys.exit(0)\n")
            env = dict(os.environ, PYTHONPATH=decoy)
            r = subprocess.run([sys.executable, str(hook)],
                               input=_stdin("a" * (gate.MAX_COMMAND_CHARS + 1), True),
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)  # the real helper's cap, not the decoy's allow
        self.assertIn(": cap: ", r.stderr)

    def test_symlinked_deploy_resolves_to_the_clone(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "background-pipe-gate.py"
            link.symlink_to(_HOOK)
            r = self._run(link)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class CapAndBudget(unittest.TestCase):
    def test_cap_constant(self):
        self.assertEqual(gate.MAX_COMMAND_CHARS, 262_144)
        self.assertEqual(fc.DEFAULT_MAX_COMMAND_CHARS, 65_536)  # the helper default is unchanged

    def test_over_cap_denies_real_script_foreground_and_background(self):
        # The cap is checked before decide(), so it covers foreground calls too: that is why
        # it is the main-thread 262,144 and not the 65,536 default.
        for background in (False, True):
            with self.subTest(background=background):
                r = _run_script(_stdin("a" * (gate.MAX_COMMAND_CHARS + 1), background))
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("BLOCKED (background-pipe-gate, fail-closed, T-4392): cap: ",
                              r.stderr)

    def test_at_cap_foreground_allows(self):
        r = _run_script(_stdin("a" * gate.MAX_COMMAND_CHARS, False))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    WORST_UNITS = ("|&/", "|/", "|x", "| ", "/|", "| ")

    def _worst_shapes(self):
        size = gate.MAX_COMMAND_CHARS
        for unit in self.WORST_UNITS:
            yield unit, unit * (size // len(unit))

    def test_scanner_at_the_cap_is_under_2s_cpu(self):
        # Measured ~0.5s CPU on the worst shape ('|&/') at the 262,144-character cap (T-4392
        # phase 2c). CPU time is load-insensitive; the wall-clock hard stop only catches a
        # real regression on the shared pinned core.
        import signal

        def hang(signum, frame):  # pragma: no cover - only on a regression
            raise AssertionError("scanner exceeded the 120s wall-clock hard stop at the cap")
        previous = signal.signal(signal.SIGALRM, hang)
        try:
            for unit, cmd in self._worst_shapes():
                self.assertLessEqual(len(cmd), gate.MAX_COMMAND_CHARS)
                signal.alarm(120)
                t0 = time.thread_time()
                gate.command_pipes_background_output(cmd)
                took = time.thread_time() - t0
                signal.alarm(0)
                self.assertLess(took, 2.0, f"{unit!r}: {took:.2f}s CPU at the cap")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_real_script_at_the_cap_allows_or_denies_only_on_its_own_clock(self):
        """The whole hook at the cap costs well under the budget in CPU. On a contended core the
        WALL clock can still pass DEADLINE_S; then the only acceptable outcome is the fail-closed
        deadline deny (never a silent overrun, never another cause)."""
        import resource
        unit, cmd = next(self._worst_shapes())  # '|&/', the worst measured
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.monotonic()
        r = _run_script(_stdin(cmd, True))
        took = time.monotonic() - t0
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
        self.assertLess(cpu, 2.5, f"hook CPU {cpu:.2f}s at the cap")
        if r.returncode == 0:
            self.assertEqual((r.stdout, r.stderr), ("", ""))
        else:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("BLOCKED (background-pipe-gate, fail-closed, T-4392): deadline: ",
                          r.stderr)
            self.assertGreaterEqual(took, fc.DEADLINE_S * 0.9)


if __name__ == "__main__":
    unittest.main()
