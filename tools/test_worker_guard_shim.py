"""Tests for the T-4177 worker-guard entry shim (tools/hooks/worker-guard.py).

The guard body lives in tools/hooks/worker_guard_impl.py and is tested by tools/test_worker_guard.py.
The shim in front of it exists for latency: a main-thread call (no agent_type, the allow path) must
exit 0 without importing anything, while EVERY other payload must reach the impl and behave exactly as
the pre-shim guard did.

PERMANENT invariant asserted here: shim == the impl run as a script, byte for byte (returncode,
stdout, stderr) over a curated corpus and a seeded fuzz. (The one-off "pre-shim monolith == shim"
migration proof lives in tools/worker_guard_cutover_smoke.py; against a frozen old guard it would
turn red on the next legitimate guard edit.)

Run: python3 -m unittest tools.test_worker_guard_shim -v
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools import worker_guard_cutover_smoke as smoke

_HOOKS = Path(__file__).resolve().parent / "hooks"
_SHIM = _HOOKS / smoke.SHIM_NAME
_IMPL = _HOOKS / smoke.IMPL_NAME

_DENY_RAW = json.dumps({
    "agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": "git push origin main"},
})


def _load_impl():
    spec = importlib.util.spec_from_file_location("worker_guard_impl_under_test", _IMPL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_would_allow_silently(text: str) -> bool:
    """What the pre-shim main() does with `text` when agent_type does not gate it in: parse error,
    non-dict (AttributeError in decide) and a falsy/absent agent_type all end in a silent exit 0."""
    try:
        data = json.loads(text) if text.strip() else {}
    except Exception:
        return True
    return not (isinstance(data, dict) and data.get("agent_type"))


class TestFastPathPredicate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fast = staticmethod(smoke.load_shim()._definitely_main_thread)

    def test_plain_main_thread_read_takes_the_fast_path(self):
        self.assertTrue(self.fast(json.dumps(smoke._MAIN_READ)))

    def test_empty_and_garbage_stdin_take_the_fast_path(self):
        for raw in ("", "  \n", "{", "not json", "null", "[]", "123"):
            with self.subTest(raw=raw):
                self.assertTrue(self.fast(raw))

    def test_write_payload_with_newline_and_quote_escapes_stays_fast(self):
        # Edit/Write bodies always carry \n, \" and \\ escapes; a bare "any backslash" test would
        # push every main-thread edit onto the slow path.
        raw = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": 'a\n"b"\\c\n'}})
        self.assertIn("\\n", raw)
        self.assertTrue(self.fast(raw))

    def test_any_agent_type_spelling_takes_the_slow_path(self):
        # Conservative on purpose: a present-but-falsy agent_type is still a plain allow in the
        # impl, just slower. What matters is that no truthy one is ever short-circuited.
        for value in ("implementer", "t1336-impl2", "", None, 0, False, [], {}, ["x"], 1, True):
            with self.subTest(agent_type=value):
                self.assertFalse(self.fast(json.dumps({"agent_type": value, "tool_name": "Bash"})))

    def test_escaped_key_spelling_takes_the_slow_path(self):
        # {"agent\u005ftype": ...} decodes to the key "agent_type"; the literal substring is absent.
        raw = '{"agent\\u005ftype":"implementer","tool_name":"Bash"}'
        self.assertNotIn("agent_type", raw)
        self.assertEqual(json.loads(raw)["agent_type"], "implementer")
        self.assertFalse(self.fast(raw))

    def test_any_unicode_escape_takes_the_slow_path(self):
        self.assertFalse(self.fast('{"\\u0061gent_type":"x"}'))
        self.assertFalse(self.fast('{"tool_name":"Bash","x":"\\u00e9"}'))

    def test_soundness_over_seeded_fuzz(self):
        """predicate True => the old guard ends in a silent allow; truthy agent_type => predicate False."""
        rng = random.Random(41770)
        fast = slow = truthy = 0
        for _ in range(20_000):
            raw = smoke.fuzz_payload(rng)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue  # stdin.read() raises before the predicate: exit 0 in old and new alike
            if self.fast(text):
                fast += 1
                self.assertTrue(_old_would_allow_silently(text), text[:300])
            else:
                slow += 1
            try:
                data = json.loads(text) if text.strip() else {}
            except Exception:
                continue
            if isinstance(data, dict) and data.get("agent_type"):
                truthy += 1
                self.assertFalse(self.fast(text), text[:300])
        # non-vacuous: both branches and the truthy-agent case were exercised
        self.assertGreater(fast, 1000)
        self.assertGreater(slow, 1000)
        self.assertGreater(truthy, 500)


class TestShimMatchesImplAsScript(unittest.TestCase):
    """The impl run as a script IS the pre-shim guard (its __main__ block is unchanged)."""

    def test_curated_corpus_is_byte_identical(self):
        cases = smoke.curated_corpus(full=False)
        cmp = smoke.compare_scripts(_IMPL, _SHIM, cases)
        self.assertEqual(cmp.mismatches, [])
        # non-vacuous: a comparison of all-allow outputs proves nothing
        self.assertGreater(cmp.total, 150)
        self.assertGreater(cmp.denies, 30)

    def test_seeded_fuzz_is_byte_identical(self):
        cmp = smoke.compare_scripts(_IMPL, _SHIM, smoke.fuzz_corpus(60, seed=4177))
        self.assertEqual(cmp.mismatches, [])
        self.assertGreater(cmp.denies, 2)


class TestCompareScriptsTimeoutRerun(unittest.TestCase):
    """T-5817: a pair that differs only because one side hit the guard's own T-4389 deadline on a
    loaded host is re-run serially and judged on the re-run; any other difference is final."""

    _ALLOW = smoke.Outcome(0, b"", b"")
    _DENY = smoke.Outcome(0, b'{"hookSpecificOutput": {"permissionDecision": "deny"}}', b"")
    _TIMEOUT = smoke.Outcome(0, b'{"x": "' + smoke.TIMED_OUT_MARKER + b' this command"}', b"")

    def _compare(self, script_outcomes, reruns=smoke.TIMEOUT_RERUNS):
        """`script_outcomes`: {script: [outcome per successive run of the one case]}."""
        calls = {"ref": 0, "new": 0}

        def fake_run(script, stdin, timeout=120.0):
            seq = script_outcomes[str(script)]
            out = seq[min(calls[str(script)], len(seq) - 1)]
            calls[str(script)] += 1
            return out

        with unittest.mock.patch.object(smoke, "run_script", side_effect=fake_run):
            cmp = smoke.compare_scripts(Path("ref"), Path("new"), [("c", b"{}")], workers=1,
                                        timeout_reruns=reruns)
        return cmp, calls

    def test_marker_is_the_impls_deadline_text(self):
        impl = _load_impl()
        self.assertIn(smoke.TIMED_OUT_MARKER, json.dumps(impl._TIMED_OUT).encode())

    def test_load_timeout_on_one_side_is_rerun_and_passes(self):
        cmp, calls = self._compare({"ref": [self._TIMEOUT, self._ALLOW], "new": [self._ALLOW]})
        self.assertEqual(cmp.mismatches, [])
        self.assertEqual(calls, {"ref": 2, "new": 2})

    def test_persistent_timeout_stays_a_mismatch(self):
        cmp, calls = self._compare({"ref": [self._TIMEOUT], "new": [self._ALLOW]})
        self.assertEqual(len(cmp.mismatches), 1)
        self.assertEqual(calls["ref"], 1 + smoke.TIMEOUT_RERUNS)

    def test_rerun_must_still_be_byte_identical(self):
        cmp, _ = self._compare({"ref": [self._TIMEOUT, self._DENY], "new": [self._ALLOW]})
        self.assertEqual(len(cmp.mismatches), 1)

    def test_a_real_difference_is_never_rerun(self):
        cmp, calls = self._compare({"ref": [self._DENY], "new": [self._ALLOW]})
        self.assertEqual(len(cmp.mismatches), 1)
        self.assertEqual(calls, {"ref": 1, "new": 1})

    def test_reruns_off_keeps_the_old_strictness(self):
        cmp, _ = self._compare({"ref": [self._TIMEOUT, self._ALLOW], "new": [self._ALLOW]}, reruns=0)
        self.assertEqual(len(cmp.mismatches), 1)

    # T-5838: identical timeouts are no decision, so they cannot clear a pair that once differed.
    def test_one_sided_timeout_then_both_timed_out_stays_a_mismatch(self):
        cmp, calls = self._compare({"ref": [self._TIMEOUT], "new": [self._ALLOW, self._TIMEOUT]})
        self.assertEqual(len(cmp.mismatches), 1)
        self.assertIn("never cleared", cmp.mismatches[0])
        self.assertIn("rc=0 out=b''", cmp.mismatches[0])  # the first, differing pair is shown
        self.assertEqual(calls, {"ref": 1 + smoke.TIMEOUT_RERUNS, "new": 1 + smoke.TIMEOUT_RERUNS})

    def test_one_sided_timeout_cleared_after_double_timeouts_by_a_clean_rerun(self):
        cmp, calls = self._compare({"ref": [self._TIMEOUT, self._TIMEOUT, self._ALLOW],
                                    "new": [self._ALLOW, self._TIMEOUT, self._ALLOW]})
        self.assertEqual(cmp.mismatches, [])
        self.assertEqual(calls, {"ref": 3, "new": 3})

    def test_both_sides_timed_out_alike_throughout_is_a_match(self):
        cmp, calls = self._compare({"ref": [self._TIMEOUT], "new": [self._TIMEOUT]})
        self.assertEqual(cmp.mismatches, [])
        self.assertEqual(calls["ref"], 1 + smoke.TIMEOUT_RERUNS)

    def test_both_timed_out_in_the_pool_is_judged_on_a_clean_rerun(self):
        cmp, _ = self._compare({"ref": [self._TIMEOUT, self._DENY], "new": [self._TIMEOUT, self._ALLOW]})
        self.assertEqual(len(cmp.mismatches), 1)
        cmp, _ = self._compare({"ref": [self._TIMEOUT, self._ALLOW], "new": [self._TIMEOUT, self._ALLOW]})
        self.assertEqual(cmp.mismatches, [])

    def test_a_difference_seen_on_a_rerun_is_not_cleared_by_double_timeouts(self):
        cmp, _ = self._compare({"ref": [self._TIMEOUT, self._TIMEOUT],
                                "new": [self._TIMEOUT, self._ALLOW, self._TIMEOUT]})
        self.assertEqual(len(cmp.mismatches), 1)


class TestShimAlone(unittest.TestCase):
    def test_fast_path_never_imports_the_impl_and_missing_impl_fails_loud(self):
        # run_isolation copies ONLY the shim to a temp dir: a main-thread payload must still be a
        # silent allow (the impl is not imported) and a subagent payload must exit 1 with an
        # ImportError traceback (non-blocking, loud, like today's unloadable-guard behaviour).
        self.assertEqual(smoke.run_isolation(_SHIM), [])

    def test_importing_the_shim_does_not_read_stdin_or_exit(self):
        with unittest.mock.patch.object(sys, "stdin", None):
            module = smoke.load_shim()
        self.assertTrue(callable(module._definitely_main_thread))


class TestImplMain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.impl = _load_impl()

    def setUp(self):
        # T-4389 r2: main() counts its budget from PROCESS START, and this test process started long
        # ago; pin "elapsed since start" to 0 so these in-process calls see a fresh hook process.
        patcher = unittest.mock.patch.object(self.impl, "_elapsed_since_process_start", return_value=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_main(self, *args):
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.impl.main(*args)
        return cm.exception.code, out.getvalue()

    def test_main_with_raw_never_touches_stdin(self):
        boom = unittest.mock.MagicMock()
        boom.read.side_effect = AssertionError("main(raw) must not read stdin")
        with unittest.mock.patch.object(sys, "stdin", boom):
            code, out = self._run_main(_DENY_RAW)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.impl.decide(json.loads(_DENY_RAW)))

    def test_main_without_raw_still_reads_stdin(self):
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(_DENY_RAW)):
            code, out = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.impl.decide(json.loads(_DENY_RAW)))

    def test_unparseable_raw_is_a_silent_fail_open(self):
        for raw in ("{", "", "   ", "null", "[1]"):
            with self.subTest(raw=raw):
                self.assertEqual(self._run_main(raw), (0, ""))

    def test_stdin_read_failure_is_a_silent_fail_open(self):
        broken = unittest.mock.MagicMock()
        broken.read.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        with unittest.mock.patch.object(sys, "stdin", broken):
            self.assertEqual(self._run_main(), (0, ""))


class TestShimShape(unittest.TestCase):
    def test_shim_is_executable_and_small(self):
        self.assertTrue(os.access(_SHIM, os.X_OK), "the hook is exec'd by path/mode; keep 100755")
        self.assertLess(len(_SHIM.read_text(encoding="utf-8").splitlines()), 80)

    def test_shim_module_level_imports_only_sys(self):
        # Anything else imported at module level runs on the fast path and eats the win.
        tree = ast.parse(_SHIM.read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name for n in top for a in n.names}
        self.assertEqual(names - {"sys"}, set())

    def test_body_identity_helper_accepts_main_and_docstring_edits_only(self):
        src = _IMPL.read_text(encoding="utf-8")
        self.assertEqual(smoke.body_identity_problems(src, src), [])
        edited_docstring = src.replace('"""PreToolUse hook', '"""EDITED PreToolUse hook', 1)
        self.assertEqual(smoke.body_identity_problems(src, edited_docstring), [])
        self.assertTrue(smoke.body_identity_problems(src, src.replace("16_384", "16_385", 1)))


class TestImplBytecodeCache(unittest.TestCase):
    def test_impl_is_cached_by_the_first_subagent_call_and_not_by_a_main_thread_call(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}
        with tempfile.TemporaryDirectory(prefix="wg-pyc-") as tmp:
            hooks = Path(tmp)
            for name in (smoke.SHIM_NAME, smoke.IMPL_NAME):
                (hooks / name).write_text((_HOOKS / name).read_text(encoding="utf-8"), encoding="utf-8")
            cached = Path(importlib.util.cache_from_source(str(hooks / smoke.IMPL_NAME)))
            run = lambda raw: subprocess.run(  # noqa: E731
                [sys.executable, str(hooks / smoke.SHIM_NAME)], input=raw.encode(), capture_output=True, env=env)
            self.assertEqual(run(json.dumps(smoke._MAIN_READ)).returncode, 0)
            self.assertFalse(cached.exists(), "a main-thread call must not import (or compile) the impl")
            proc = run(_DENY_RAW)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny")
            self.assertTrue(cached.exists(), "the first subagent call should leave a cached .pyc")


class TestShimInvocationForms(unittest.TestCase):
    """The shim must find ITS impl however the interpreter is started, and a decoy module of the same
    name elsewhere on sys.path must never win: a shadowed impl silently loses every subagent deny.

    Regression for the r2 review finding: with PYTHONSAFEPATH=1 and PYTHONPATH=<decoy>:<real hooks dir>,
    the hooks dir is already on sys.path, so a guarded `if here not in sys.path: insert(0, here)` skipped
    the insert and the decoy (earlier on the path) was imported instead.
    """

    _DECOY_MARKER = "DECOY-IMPL-RAN"

    @staticmethod
    def _run(argv_prefix=(), *, script=_SHIM, env=None, cwd=None, raw=_DENY_RAW):
        return subprocess.run(
            [sys.executable, *argv_prefix, str(script)], input=raw.encode(), capture_output=True,
            env=env, cwd=cwd,
        )

    def _assert_denies(self, proc):
        self.assertNotIn(self._DECOY_MARKER.encode(), proc.stdout + proc.stderr, "a decoy impl shadowed the real one")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        self.assertTrue(json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny", proc.stdout)

    def test_decoy_earlier_on_pythonpath_cannot_shadow_the_impl_under_safepath(self):
        with tempfile.TemporaryDirectory(prefix="wg-decoy-") as decoy:
            (Path(decoy) / smoke.IMPL_NAME).write_text(
                f"import sys\ndef main(raw=None):\n    print({self._DECOY_MARKER!r})\n    sys.exit(0)\n", encoding="utf-8")
            env = {**os.environ, "PYTHONSAFEPATH": "1", "PYTHONPATH": os.pathsep.join([decoy, str(_HOOKS)])}
            self._assert_denies(self._run(env=env))
            # the same env for a main-thread payload: still a silent allow, decoy untouched
            main = self._run(env=env, raw=json.dumps(smoke._MAIN_READ))
            self.assertEqual((main.returncode, main.stdout, main.stderr), (0, b"", b""))

    def test_decoy_on_pythonpath_without_safepath_cannot_shadow_either(self):
        with tempfile.TemporaryDirectory(prefix="wg-decoy-") as decoy:
            (Path(decoy) / smoke.IMPL_NAME).write_text(
                f"import sys\ndef main(raw=None):\n    print({self._DECOY_MARKER!r})\n    sys.exit(0)\n", encoding="utf-8")
            env = {**os.environ, "PYTHONPATH": decoy}
            self._assert_denies(self._run(env=env))

    def test_invocation_forms_all_reach_the_impl(self):
        base_env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        with tempfile.TemporaryDirectory(prefix="wg-forms-") as tmp:
            link = Path(tmp) / "linked-guard.py"
            link.symlink_to(_SHIM)
            forms = {
                "plain": dict(),
                "other-cwd": dict(cwd=tmp),
                "symlinked-script": dict(script=link),
                "symlinked-script-other-cwd": dict(script=link, cwd=tmp),
                "isolated -I": dict(argv_prefix=("-I",)),
                "no-site -S": dict(argv_prefix=("-S",)),
                "safe-path -P": dict(argv_prefix=("-P",)),
                "PYTHONSAFEPATH=1": dict(env={**base_env, "PYTHONSAFEPATH": "1"}),
            }
            for label, kwargs in forms.items():
                with self.subTest(form=label):
                    self._assert_denies(self._run(**kwargs))
                    main = self._run(raw=json.dumps(smoke._MAIN_READ), **kwargs)
                    self.assertEqual((main.returncode, main.stdout, main.stderr), (0, b"", b""))


_SUB_BASH = json.dumps({"agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}})
_TIMEOUT_REASON = "worker-guard timed out deciding this command; refused (fail-closed). Split or simplify the command."


def _one_deny(test: unittest.TestCase, out) -> dict:
    """stdout is exactly one well-formed deny JSON line in the impl's deny shape."""
    text = out.decode() if isinstance(out, bytes) else out
    lines = text.splitlines()
    test.assertEqual(len(lines), 1, text[:500])
    payload = json.loads(lines[0])
    test.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
    test.assertEqual(set(payload), {"hookSpecificOutput"})
    test.assertEqual(set(payload["hookSpecificOutput"]),
                     {"hookEventName", "permissionDecision", "permissionDecisionReason"})
    test.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "PreToolUse")
    return payload


class TestFailClosedDeadline(unittest.TestCase):
    """T-4389: a SUBAGENT decision that outlives the guard's own clock is a deny (the harness hook
    timeout is 600s and non-blocking, i.e. fail-open). The main thread never runs under it."""

    @classmethod
    def setUpClass(cls):
        cls.impl = _load_impl()
        cls.real_elapsed = staticmethod(cls.impl._elapsed_since_process_start)  # before setUp patches it

    def setUp(self):
        # T-4389 r2: main() counts its budget from PROCESS START, and this test process started long
        # ago; pin "elapsed since start" to 0 so these in-process calls see a fresh hook process.
        patcher = unittest.mock.patch.object(self.impl, "_elapsed_since_process_start", return_value=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_main(self, raw, **kwargs):
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdout", out):
            with self.assertRaises(SystemExit) as cm:
                self.impl.main(raw, **kwargs)
        return cm.exception.code, out.getvalue()

    def _timed_main(self, decide, deadline):
        import time
        with unittest.mock.patch.object(self.impl, "decide", decide):
            start = time.monotonic()
            code, out = self._run_main(_SUB_BASH, deadline=deadline)
            return code, out, time.monotonic() - start

    def test_deadline_is_a_5s_module_constant(self):
        self.assertEqual(self.impl.DECIDE_DEADLINE_S, 5.0)

    def test_slow_decides_yield_exactly_one_deny_within_deadline_plus_1s(self):
        import re
        import time

        def sleeper(data):
            time.sleep(30)

        def spinner(data):  # pure-Python loop that swallows ordinary exceptions
            while True:
                try:
                    sum(range(1000))
                except Exception:
                    pass

        def backtracker(data):  # catastrophic backtracking inside the C regex engine
            return re.match(r"(a+)+$", "a" * 60 + "b")

        for decide in (sleeper, spinner, backtracker):
            with self.subTest(decide=decide.__name__):
                code, out, elapsed = self._timed_main(decide, 0.3)
                self.assertEqual(code, 0)
                payload = _one_deny(self, out)
                self.assertEqual(payload, self.impl._TIMED_OUT)
                self.assertIn(_TIMEOUT_REASON, payload["hookSpecificOutput"]["permissionDecisionReason"])
                self.assertLess(elapsed, 0.3 + 1.0)

    def test_alarm_is_disarmed_and_handler_restored_after_every_outcome(self):
        import signal
        import time
        sentinel = lambda *a: None  # noqa: E731
        old = signal.signal(signal.SIGALRM, sentinel)
        try:
            for decide in (lambda d: time.sleep(5), lambda d: None, self.impl._reason, lambda d: 1 / 0):
                self._timed_main(decide, 0.2)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
                self.assertIs(signal.getsignal(signal.SIGALRM), sentinel)
        finally:
            signal.signal(signal.SIGALRM, old)

    def test_fast_decisions_are_unchanged(self):
        self.assertEqual(self._run_main(_SUB_BASH), (0, ""))
        code, out = self._run_main(_DENY_RAW)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), self.impl.decide(json.loads(_DENY_RAW)))
        self.assertIn("git push", out)

    def test_subagent_decide_exception_is_a_deny(self):
        # T-4505 (orchestrator-ratified): a subagent decide() that raises denies, naming only the class
        # (the message could carry the command), with one stderr line; never a fail-open.
        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err):
            code, out, _ = self._timed_main(lambda d: 1 / 0, 5.0)
        self.assertEqual(code, 0)
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("T-4505", reason)
        self.assertIn("ZeroDivisionError", reason)
        self.assertNotIn("division by zero", reason)
        self.assertEqual(err.getvalue(), "worker-guard: fail-closed: decide raised ZeroDivisionError (T-4505)\n")

    def test_subagent_decide_exception_on_the_thread_fallback_is_a_deny(self):
        # the no-SIGALRM path hands the exception back to main(), which now denies on it
        with unittest.mock.patch.object(self.impl, "decide", lambda d: [][1]), \
                unittest.mock.patch("threading.current_thread", return_value=object()), \
                unittest.mock.patch.object(sys, "stderr", io.StringIO()):
            code, out = self._run_main(_SUB_BASH, deadline=5.0)
        self.assertEqual(code, 0)
        self.assertIn("IndexError", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_main_thread_decide_exception_stays_fail_open(self):
        main_bash = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}})
        with unittest.mock.patch.object(self.impl, "decide", lambda d: 1 / 0):
            code, out = self._run_main(main_bash)
        self.assertEqual((code, out), (0, ""))

    # --- T-5224: subagent decisions run under an address-space cap -------------------------------

    def test_memory_cap_is_1gib_during_a_subagent_decide_and_restored_after(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_AS)
        seen = []
        with unittest.mock.patch.object(self.impl, "decide",
                                        lambda d: seen.append(resource.getrlimit(resource.RLIMIT_AS))):
            self._run_main(_SUB_BASH, deadline=5.0)
        self.assertEqual(self.impl.DECIDE_MEMORY_CAP_BYTES, 1 << 30)
        # T-5329: headroom over the current VSZ, so 1 GiB <= cap <= 1 GiB + this process's size
        self.assertGreaterEqual(seen[0][0], 1 << 30)
        self.assertLessEqual(seen[0][0], (1 << 30) + self.impl._current_vsz_bytes() + (64 << 20))
        self.assertEqual(seen[0][1], before[1])  # the hard limit is never touched
        self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)

    def test_memory_cap_never_raises_a_lower_existing_limit(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_AS)
        if before[1] != resource.RLIM_INFINITY and before[1] < (1 << 30):
            self.skipTest("hard limit already below the cap")
        low = 512 << 20
        resource.setrlimit(resource.RLIMIT_AS, (low, before[1]))
        try:
            seen = []
            with unittest.mock.patch.object(self.impl, "decide",
                                            lambda d: seen.append(resource.getrlimit(resource.RLIMIT_AS)[0])):
                self._run_main(_SUB_BASH, deadline=5.0)
            self.assertEqual(seen, [low])
            self.assertEqual(resource.getrlimit(resource.RLIMIT_AS)[0], low)
        finally:
            resource.setrlimit(resource.RLIMIT_AS, before)

    def test_main_thread_decide_runs_uncapped(self):
        import resource
        main_bash = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}})
        before = resource.getrlimit(resource.RLIMIT_AS)
        seen = []
        with unittest.mock.patch.object(self.impl, "decide",
                                        lambda d: seen.append(resource.getrlimit(resource.RLIMIT_AS))):
            self._run_main(main_bash)
        self.assertEqual(seen, [before])

    def test_t5329_cap_does_not_starve_an_already_large_process(self):
        """An absolute 1 GiB cap hung CI: in a fat xdist worker the thread fallback could not
        start a thread. Reproduce in a subprocess with ~1.2 GiB of calloc'd (virtual-only)
        ballast; the thread-fallback exception test must still yield its IndexError deny."""
        code = (
            "import sys, unittest; sys.path.insert(0, sys.argv[1]); ballast = bytes(1200 << 20)\n"
            "from tools import test_worker_guard_shim as m\n"
            "name = 'test_subagent_decide_exception_on_the_thread_fallback_is_a_deny'\n"
            "cls = [c for c in vars(m).values() if isinstance(c, type) and hasattr(c, name)][0]\n"
            "r = unittest.TextTestRunner(stream=open('/dev/null', 'w')).run(unittest.TestSuite([cls(name)]))\n"
            "sys.exit(0 if r.wasSuccessful() else 1)\n"
        )
        repo = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run([sys.executable, "-c", code, repo], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_runaway_allocation_is_a_memoryerror_deny_and_the_cap_is_restored(self):
        import resource
        before = resource.getrlimit(resource.RLIMIT_AS)

        def hog(data):
            # bytes(n) is calloc'd: virtual only, so even uncapped this touches no pages
            return bytes(3 << 30)

        err = io.StringIO()
        with unittest.mock.patch.object(sys, "stderr", err):
            code, out, _ = self._timed_main(hog, 5.0)
        self.assertEqual(code, 0)
        reason = _one_deny(self, out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("T-4505", reason)
        self.assertIn("MemoryError", reason)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_AS), before)

    def test_runaway_allocation_on_the_thread_fallback_is_a_deny_too(self):
        def hog(data):
            return bytes(3 << 30)

        with unittest.mock.patch.object(self.impl, "decide", hog), \
                unittest.mock.patch("threading.current_thread", return_value=object()), \
                unittest.mock.patch.object(sys, "stderr", io.StringIO()):
            code, out = self._run_main(_SUB_BASH, deadline=5.0)
        self.assertEqual(code, 0)
        self.assertIn("MemoryError", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_nested_command_decides_normally_under_the_cap_in_bounded_memory(self):
        import tracemalloc
        # T-5140 r2 (D-a): nesting past the 128-frame scan cap fails closed, so the deep
        # shape (3 KB) is now the structural deny; one under the cap is still an allow.
        for depth, want in ((400, self.impl._SHELL_NESTING_TOO_DEEP), (120, None)):
            cmd = "echo " + "$(echo " * depth + "x" + ")" * depth
            data = {"agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/tmp"}
            tracemalloc.start()
            try:
                with self.impl._memory_cap():
                    verdict = self.impl.decide(data)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            self.assertEqual(verdict, want, depth)
            self.assertLess(peak, 20_000_000)

    def test_over_long_command_is_denied_fast_without_parsing(self):
        import time
        cmd = "echo " + "$(echo " * 20000 + "x" + ")" * 20000  # ~260 KB, far over BASH_COMMAND_MAX_CHARS
        self.assertGreater(len(cmd), self.impl.BASH_COMMAND_MAX_CHARS)
        start = time.monotonic()
        verdict = self.impl.decide({"agent_type": "implementer", "tool_name": "Bash",
                                    "tool_input": {"command": cmd}, "cwd": "/tmp"})
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertEqual(verdict, self.impl._TOO_LONG)

    def test_override_can_only_shorten_the_deadline(self):
        seen = []
        with unittest.mock.patch.object(self.impl, "_decide_by_deadline", lambda d, limit: seen.append(limit)):
            for deadline in (None, 999.0, 20.0, 5.0, 5.01, 0.5, 0, -1, float("nan"), float("inf")):
                self._run_main(_SUB_BASH, deadline=deadline)
        self.assertEqual(seen, [5.0, 5.0, 5.0, 5.0, 5.0, 0.5, 5.0, 5.0, 5.0, 5.0])

    def _armed_for(self, elapsed, deadline=None):
        """(the timer main() arms, or None if it never armed, and the payload) with the process
        having started `elapsed` seconds ago."""
        seen = []
        with unittest.mock.patch.object(self.impl, "_elapsed_since_process_start", return_value=elapsed), \
                unittest.mock.patch.object(self.impl, "_decide_by_deadline",
                                           lambda d, limit: seen.append(limit)), \
                unittest.mock.patch.object(self.impl, "decide", side_effect=AssertionError("unclocked decide")):
            _, out = self._run_main(_SUB_BASH, deadline=deadline)
        return (seen[0] if seen else None), out

    def test_slow_startup_shortens_the_armed_timer(self):
        for elapsed, deadline, armed in ((0.0, None, 5.0), (1.5, None, 3.5), (3.0, None, 2.0),
                                         (4.99, None, 0.05), (0.4, 1.0, 0.6)):
            with self.subTest(elapsed=elapsed, deadline=deadline):
                limit, out = self._armed_for(elapsed, deadline)
                self.assertAlmostEqual(limit, armed, places=6)
                self.assertEqual(out, "")

    def test_spent_budget_denies_immediately_without_deciding(self):
        for elapsed, deadline in ((5.0, None), (6.0, None), (600.0, None), (1.0, 1.0), (float("nan"), None)):
            with self.subTest(elapsed=elapsed, deadline=deadline):
                limit, out = self._armed_for(elapsed, deadline)
                self.assertIsNone(limit)
                self.assertEqual(_one_deny(self, out), self.impl._TIMED_OUT)

    def test_process_start_reader(self):
        # a fresh process: small and non-negative, and it counts time spent before the reader ran
        code = ("import importlib.util, json, sys, time; time.sleep(0.5); "
                f"spec = importlib.util.spec_from_file_location('m', {str(_IMPL)!r}); "
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
                "print(json.dumps(m._elapsed_since_process_start()))")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
        elapsed = json.loads(proc.stdout)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 5.0)
        # without /proc: the shim's monotonic stamp; with neither, 0.0
        import time
        with unittest.mock.patch("builtins.open", side_effect=OSError("no /proc")):
            self.assertGreaterEqual(self.real_elapsed(time.monotonic() - 2.0), 2.0)
            self.assertEqual(self.real_elapsed(None), 0.0)

    def test_main_thread_payload_never_runs_under_the_clock(self):
        import time
        calls = []
        with unittest.mock.patch.object(self.impl, "_decide_by_deadline", side_effect=AssertionError("clocked")):
            with unittest.mock.patch.object(self.impl, "decide", lambda d: calls.append(d) or time.sleep(0.4)):
                for raw in ('{"tool_name":"Bash","tool_input":{"command":"\\u0067it push"}}',
                            '{"agent_type":"","tool_name":"Bash"}', "[1]"):
                    self.assertEqual(self._run_main(raw, deadline=0.1), (0, ""))
        self.assertEqual(len(calls), 3)

    def test_checked_deadline_fallback_without_sigalrm(self):
        import threading
        import time
        results = {}

        def call(key, decide):
            with unittest.mock.patch.object(self.impl, "decide", decide):
                results[key] = self.impl._decide_by_deadline({"agent_type": "x"}, 0.3)

        cases = {"slow": lambda d: time.sleep(5), "fast": lambda d: None, "deny": self.impl._reason}
        start = time.monotonic()
        for key, decide in cases.items():  # off the main thread: SIGALRM is unusable there
            t = threading.Thread(target=call, args=(key, decide))
            t.start()
            t.join(5)
        self.assertLess(time.monotonic() - start, 0.3 + 1.0 + 1.0)
        self.assertEqual(results["slow"], self.impl._TIMED_OUT)
        self.assertIsNone(results["fast"])
        self.assertEqual(results["deny"], self.impl._reason({"agent_type": "x"}))
        # a decide() exception is handed back, so main()'s fail-open handling is unchanged
        with unittest.mock.patch.object(self.impl, "decide", lambda d: 1 / 0):
            box = {}
            t = threading.Thread(target=lambda: box.setdefault(
                "e", self._raises(lambda: self.impl._decide_by_deadline({}, 1.0))))
            t.start()
            t.join(5)
        self.assertIs(box["e"], ZeroDivisionError)

    @staticmethod
    def _raises(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            return type(exc)
        return None


class TestDeadlineUnderHarnessTimeouts(unittest.TestCase):
    """T-4389 r1 M1 INVARIANT: the guard's own clock must fire well under every hook timeout it is
    wired under, or the harness kills it first and (Claude Code and Codex alike, T-4321) the call
    PROCEEDS. Margin 2.5s covers interpreter start, the impl import and the pre-clock json parse.

    Sources read here: Codex's wiring, tools/harness_adapter/codex.py `_WORKER_GUARD_BLOCK`. The
    Claude Code seats' worker-guard timeouts are NOT in any repo source: they live in each
    account's own ~/.claude/settings.json. They are per-account settings, currently unset (so the
    600s default applies) on at least suborch1, and planned at 30 once each hook's self-deadline
    lands; they cannot be pinned from here.

    DECIDE_DEADLINE_S is the TOTAL budget from process start (T-4389 r2), so the margin covers only
    the timer's own lateness and the print/exit, with room for a loaded box."""

    MARGIN_S = 4.5

    def test_deadline_plus_margin_is_under_the_codex_hook_timeout(self):
        import tomllib
        from tools.harness_adapter import codex
        block = tomllib.loads(codex._WORKER_GUARD_BLOCK.format(command='"x"'))
        hooks = [h for group in block["hooks"]["PreToolUse"] for h in group["hooks"]]
        self.assertTrue(hooks)
        impl = _load_impl()
        for hook in hooks:
            self.assertIn("timeout", hook, "an unset timeout would silently fall back to the harness default")
            self.assertLess(impl.DECIDE_DEADLINE_S + self.MARGIN_S, hook["timeout"],
                            "lower DECIDE_DEADLINE_S or raise the Codex hook timeout together")

    def test_codex_rendered_command_runs_the_real_shim_isolated(self):
        """T-4395 / T-4397 A: the EXACT argv codex runs (`/usr/bin/python3 -I <home>/pantheon-config/
        tools/hooks/worker-guard-v2.py`) reaches the impl and decides. TestShimInvocationForms covers the
        `-I` form with sys.executable; this pins the adapter's own rendered command end to end."""
        import shlex
        from tools.harness_adapter import codex
        with tempfile.TemporaryDirectory(prefix="wg-codex-cmd-") as home:
            (Path(home) / "pantheon-config").symlink_to(_HOOKS.parent.parent)
            argv = shlex.split(codex.worker_guard_command(Path(home)))
            self.assertEqual(argv[:2], ["/usr/bin/python3", "-I"])
            deny = subprocess.run(argv, input=_DENY_RAW.encode(), capture_output=True, timeout=60)
            self.assertEqual((deny.returncode, deny.stderr), (0, b""))
            self.assertEqual(json.loads(deny.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
            # Recovery (T-4395 r1): -I implies -P, dropping the script dir from sys.path. A SUBAGENT
            # payload that should be ALLOWED must still reach the impl and come back a silent allow
            # (an ImportError would be exit 1 + a traceback, i.e. a fail-open, not this).
            allow = subprocess.run(argv, input=_SUB_BASH.encode(), capture_output=True, timeout=60)
            self.assertEqual((allow.returncode, allow.stdout, allow.stderr), (0, b"", b""))
            main = subprocess.run(argv, input=json.dumps(smoke._MAIN_READ).encode(), capture_output=True,
                                  timeout=60)
            self.assertEqual((main.returncode, main.stdout, main.stderr), (0, b"", b""))


class TestFailClosedEndToEnd(unittest.TestCase):
    """Through the real shim, in a subprocess, with a copied impl whose decide() is slow and whose
    DECIDE_DEADLINE_S constant is shortened IN THE FILE (no env override exists to use)."""

    DEADLINE = 1.0
    SLOW = 3.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="t4389-wg-")
        hooks = Path(cls.tmp.name)
        (hooks / smoke.SHIM_NAME).write_text(_SHIM.read_text(encoding="utf-8"), encoding="utf-8")
        impl = _IMPL.read_text(encoding="utf-8").replace("\nif __name__ == \"__main__\":\n    main()\n", "\n")
        impl += (f"\nDECIDE_DEADLINE_S = {cls.DEADLINE}\n\n\ndef decide(data):\n"
                 f"    import time\n    time.sleep({cls.SLOW})\n    return None\n")
        (hooks / smoke.IMPL_NAME).write_text(impl, encoding="utf-8")
        # Pre-compile, so the timed runs measure the clock, not a first-call compile on a loaded box.
        import py_compile
        py_compile.compile(str(hooks / smoke.IMPL_NAME), doraise=True)
        cls.shim = hooks / smoke.SHIM_NAME

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, raw, env=None):
        import time
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(self.shim)], input=raw.encode(), capture_output=True,
                              timeout=60, env=env)
        return proc, time.monotonic() - start

    def test_slow_subagent_decision_is_one_deny_within_deadline_plus_1s(self):
        proc, elapsed = self._run(_SUB_BASH)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        payload = _one_deny(self, proc.stdout)
        self.assertIn(_TIMEOUT_REASON, payload["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertLess(elapsed, self.DEADLINE + 1.0)

    def test_environment_cannot_lengthen_the_deadline(self):
        env = {**os.environ, "DECIDE_DEADLINE_S": "600", "WORKER_GUARD_DEADLINE": "600",
               "PANTHEON_TEST": "1", "PYTEST_CURRENT_TEST": "x"}
        proc, elapsed = self._run(_SUB_BASH, env=env)
        _one_deny(self, proc.stdout)
        self.assertLess(elapsed, self.SLOW)  # cut off by the in-file constant, not the env

    def test_main_thread_escaped_payload_reaches_the_slow_impl_but_is_never_denied(self):
        raw = '{"tool_name":"Bash","tool_input":{"command":"\\u0067it push"}}'  # \u: not the fast path
        proc, elapsed = self._run(raw)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        self.assertGreaterEqual(elapsed, self.SLOW)  # it ran past the deadline, unclocked

    def test_main_thread_huge_payload_is_a_silent_allow_with_no_import(self):
        with tempfile.TemporaryDirectory(prefix="t4389-wg-alone-") as tmp:
            lone = Path(tmp) / smoke.SHIM_NAME  # shim only: any import of the impl would exit 1
            lone.write_text(_SHIM.read_text(encoding="utf-8"), encoding="utf-8")
            for payload in ({"tool_name": "Bash", "tool_input": {"command": "echo " + "a" * 2_000_000}},
                            {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x", "content": "y\n" * 1_000_000}}):
                with self.subTest(tool=payload["tool_name"]):
                    proc = subprocess.run([sys.executable, str(lone)], input=json.dumps(payload).encode(),
                                          capture_output=True, timeout=60)
                    self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))

    def test_real_impl_size_cap_through_the_shim(self):
        over = json.dumps({"agent_type": "implementer", "tool_name": "Bash", "tool_input": {"command": "x" * 65_537}})
        proc = subprocess.run([sys.executable, str(_SHIM)], input=over.encode(), capture_output=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertIn("longer than 65536", _one_deny(self, proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        big_write = json.dumps({"agent_type": "implementer", "tool_name": "Write",
                                "tool_input": {"file_path": "/tmp/t4389/big.txt", "content": "z" * 1_000_000}})
        proc = subprocess.run([sys.executable, str(_SHIM)], input=big_write.encode(), capture_output=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"", b""))
        normal = subprocess.run([sys.executable, str(_SHIM)], input=_SUB_BASH.encode(), capture_output=True, timeout=60)
        self.assertEqual((normal.returncode, normal.stdout, normal.stderr), (0, b"", b""))


class TestSmokeMigrationOptIn(unittest.TestCase):
    """T-5324: the frozen pre-shim comparison is opt-in; the default smoke run never starts it."""

    def _run(self, *argv: str) -> tuple[int, list]:
        calls = []

        def fake_migration(*args, **kwargs):
            calls.append(args)
            return [], [], smoke.PRE_SHIM_SHA

        with unittest.mock.patch.object(smoke, "run_migration", fake_migration), \
                unittest.mock.patch.object(smoke, "run_probes", return_value=[]), \
                unittest.mock.patch.object(smoke, "run_isolation", return_value=[]), \
                unittest.mock.patch("sys.stdout", io.TextIOWrapper(io.BytesIO())):
            rc = smoke.main(list(argv))
        return rc, calls

    def test_default_run_skips_migration(self):
        self.assertEqual(self._run(), (0, []))

    def test_old_skip_flag_still_accepted(self):
        self.assertEqual(self._run("--skip-migration"), (0, []))

    def test_migration_flag_runs_it(self):
        rc, calls = self._run("--migration")
        self.assertEqual((rc, len(calls)), (0, 1))


if __name__ == "__main__":
    unittest.main()
