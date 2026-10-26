#!/usr/bin/env python3
"""mcp-list-guard wrapped by tools/hooks/_failclosed.py (T-4392 phase 2a).

  * CI CANARY: an ordinary-command corpus must stay ALL-ALLOW with the poller faked LIVE, so a
    throw-on-everything or deny-on-everything bug cannot merge. The corpus is the checked-in
    tools/fixtures/failclosed_canary_bash.json PLUS every literal the existing guard tests
    assert is NOT an invocation (harvested from tools/test_mcp_list_guard.py by AST).
  * IMPORT FAILURE of _failclosed denies (exit 2), including a broken helper; a decoy helper
    on PYTHONPATH cannot win.
  * The real script end to end: the rule deny is unchanged; the 262,144-character cap denies; the worst
    scanner shape at the cap stays well inside the budget.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
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
_HOOK = _HOOKS / "mcp-list-guard.py"
_CORPUS = _TOOLS / "fixtures" / "failclosed_canary_bash.json"

sys.path.insert(0, str(_HOOKS))
import _failclosed as fc  # noqa: E402

_spec = importlib.util.spec_from_file_location("mcp_list_guard_fc", _HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _harvested_negatives() -> list[str]:
    """String literals passed to assertFalse(guard.command_targets_mcp_list(...)) in the guard's
    own test file: every shape its authors pinned as NOT an invocation."""
    tree = ast.parse((_TOOLS / "test_mcp_list_guard.py").read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "assertFalse"
                and node.args and isinstance(node.args[0], ast.Call)
                and getattr(node.args[0].func, "attr", None) == "command_targets_mcp_list"
                and node.args[0].args and isinstance(node.args[0].args[0], ast.Constant)
                and isinstance(node.args[0].args[0].value, str)):
            out.append(node.args[0].args[0].value)
    return out


def _corpus() -> list[str]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))["commands"] + _harvested_negatives()


def _bash(command: str) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


class _FakeLivePoller:
    """A sandbox HOME whose bot.pid points at a live self-owned process with `bun` + `server.ts`
    in its argv (the own_poller_live() substrings), as the bring-up prover does."""

    def __enter__(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        pid_path = self.home / ".claude" / "channels" / "telegram" / "bot.pid"
        pid_path.parent.mkdir(parents=True)
        self.proc = subprocess.Popen(["bun_probe_server.ts", "300"], executable="/bin/sleep",
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid_path.write_text(str(self.proc.pid), encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home))
        self.env.pop("PYTHONPATH", None)
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self._td.cleanup()

    def run(self, command: str, hook: Path = _HOOK, env: dict | None = None):
        return subprocess.run([sys.executable, str(hook)], input=_bash(command), capture_output=True,
                              text=True, env=env or self.env, timeout=60)


class CanaryAllAllow(unittest.TestCase):
    def test_corpus_is_substantial(self):
        corpus = _corpus()
        self.assertGreaterEqual(len(corpus), 150)
        self.assertGreaterEqual(len(_harvested_negatives()), 10)

    def test_every_ordinary_command_allows_in_process_with_a_live_poller(self):
        with mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0):
            for command in _corpus():
                with self.subTest(command=command[:80]):
                    code, out, err, _ = fc.evaluate(
                        "mcp-list-guard", lambda d: guard.decide(d, True),
                        max_command_chars=guard.MAX_COMMAND_CHARS, stdin=io.StringIO(_bash(command)))
                    self.assertEqual((code, out, err), (0, "", ""))

    def test_real_script_sample_allows_with_a_live_poller(self):
        corpus = _corpus()
        sample = corpus[:: max(1, len(corpus) // 30)]
        with _FakeLivePoller() as fp:
            for command in sample:
                with self.subTest(command=command[:80]):
                    r = fp.run(command)
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            # ... while a real invocation still gets the unchanged rule deny (JSON, exit 0).
            r = fp.run("claude -p hi")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_poller_absent_allows_invocation_silently(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            r = subprocess.run([sys.executable, str(_HOOK)], input=_bash("claude mcp list"),
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class ImportFailureDenies(unittest.TestCase):
    def _copy_hook(self, td: Path) -> Path:
        dst = td / "mcp-list-guard.py"
        shutil.copyfile(_HOOK, dst)
        return dst

    def _run(self, hook: Path, env=None):
        env = dict(os.environ if env is None else env)
        env.setdefault("HOME", str(hook.parent))
        return subprocess.run([sys.executable, str(hook)], input=_bash("ls"), capture_output=True,
                              text=True, env=env, timeout=60)

    def test_missing_helper_denies_with_recovery_hint(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(self._copy_hook(Path(td)))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (mcp-list-guard, fail-closed, T-4392): cannot import _failclosed", r.stderr)
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
            env = dict(os.environ, PYTHONPATH=decoy, HOME=td)
            r = subprocess.run([sys.executable, str(hook)],
                               input=_bash("a" * (guard.MAX_COMMAND_CHARS + 1)),
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)  # the real helper's cap, not the decoy's allow
        self.assertIn(": cap: ", r.stderr)

    def test_symlinked_deploy_resolves_to_the_clone(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "mcp-list-guard.py"
            link.symlink_to(_HOOK)
            r = self._run(link)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class ConcurrentSpawnsNeverDeny(unittest.TestCase):
    """mason addition (T-4412 context): CI has shown load-related mcp-list-guard flakes under
    pytest-xdist, and the wrap turns a slow start into a DENY. Spawn the REAL wrapped hook N at a
    time on ordinary allow-expected payloads with the poller faked LIVE: every run must exit 0 with
    no output. The spawn-to-exit distribution (an upper bound on process-start-to-decision) is
    printed for the record; run with `-s` to see it."""

    PARALLEL = 8
    ROUNDS = 25

    def test_parallel_spawns_all_allow(self):
        from concurrent.futures import ThreadPoolExecutor
        corpus = json.loads(_CORPUS.read_text(encoding="utf-8"))["commands"]

        with _FakeLivePoller() as fp:
            def one(i):
                command = corpus[i % len(corpus)]
                t0 = time.monotonic()
                r = fp.run(command)
                return command, r, time.monotonic() - t0

            results = []
            with ThreadPoolExecutor(max_workers=self.PARALLEL) as pool:
                for rnd in range(self.ROUNDS):
                    base = rnd * self.PARALLEL
                    results.extend(pool.map(one, range(base, base + self.PARALLEL)))

        took = sorted(t for _, _, t in results)
        n = len(took)
        summary = (f"[T-4392 2a concurrency] {n} spawns ({self.PARALLEL} parallel x {self.ROUNDS}): "
                   f"min={took[0]:.3f}s p50={took[n // 2]:.3f}s p95={took[int(n * 0.95)]:.3f}s "
                   f"max={took[-1]:.3f}s budget={fc.DEADLINE_S:g}s")
        print(summary, file=sys.stderr)
        bad = [(c[:60], r.returncode, r.stdout[:120], r.stderr[:300]) for c, r, _ in results
               if (r.returncode, r.stdout, r.stderr) != (0, "", "")]
        self.assertEqual(bad, [], summary)


class CapAndBudget(unittest.TestCase):
    MENTION = " ls /home/claude"

    def test_cap_constant(self):
        self.assertEqual(guard.MAX_COMMAND_CHARS, 262_144)
        self.assertEqual(fc.DEFAULT_MAX_COMMAND_CHARS, 65_536)  # the helper default is unchanged

    def test_over_cap_denies_real_script(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            r = subprocess.run([sys.executable, str(_HOOK)],
                               input=_bash("a" * (guard.MAX_COMMAND_CHARS + 1)),
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (mcp-list-guard, fail-closed, T-4392): cap: ", r.stderr)

    def _worst_shapes(self):
        size = guard.MAX_COMMAND_CHARS
        units = ["; ", "(/", "\n", "a=1\n", ";a", "cat <<A\n", "A=1 ", "env "]
        for unit in units:
            body = unit * ((size - len(self.MENTION)) // len(unit))
            yield unit, body + self.MENTION

    def test_scanner_at_the_cap_is_under_2s_cpu(self):
        # Measured 0.99s CPU on the worst shape ('; ') at the 262,144-character cap (T-4392 phase 2a). CPU time
        # (time.thread_time) is load-insensitive; the per-shape wall-clock hard stop is generous
        # because the pinned core is shared with the fleet (it only catches a real regression).
        import signal

        def hang(signum, frame):  # pragma: no cover - only on a regression
            raise AssertionError("scanner exceeded the 120s wall-clock hard stop at the cap")
        previous = signal.signal(signal.SIGALRM, hang)
        try:
            for unit, cmd in self._worst_shapes():
                self.assertLessEqual(len(cmd), guard.MAX_COMMAND_CHARS)
                signal.alarm(120)
                t0 = time.thread_time()
                guard.command_targets_mcp_list(cmd)
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
        unit, cmd = next(self._worst_shapes())  # '; ', the worst measured
        with _FakeLivePoller() as fp:
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
            t0 = time.monotonic()
            r = fp.run(cmd)
            took = time.monotonic() - t0
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
        self.assertLess(cpu, 2.5, f"hook CPU {cpu:.2f}s at the cap")
        if r.returncode == 0:
            self.assertEqual((r.stdout, r.stderr), ("", ""))
        else:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("BLOCKED (mcp-list-guard, fail-closed, T-4392): deadline: ", r.stderr)
            self.assertGreaterEqual(took, fc.DEADLINE_S * 0.9)


if __name__ == "__main__":
    unittest.main()
