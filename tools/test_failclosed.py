#!/usr/bin/env python3
"""Tests for tools/hooks/_failclosed.py (T-4392 phase 2a): the fail-closed runner and the
break-glass (Recovery's design). Break-glass fixtures are REAL temp dirs with REAL ownership: files
owned by the test user are rejected as same-uid for real; the acceptance path injects the owner
policy (root_uid / hook_uids / allowed_owner_uids), since the test cannot chown to root."""

from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HOOKS = Path(__file__).resolve().parent / "hooks"
sys.path.insert(0, str(_HOOKS))
import _failclosed as fc  # noqa: E402

_ME = os.getuid()
_OTHER = 2_000_000_000  # a uid nobody has: stands in for "the hook process" in injected policies


def _payload(command="ls", tool="Bash"):
    return io.StringIO(json.dumps({"tool_name": tool, "tool_input": {"command": command}}))


def _allow(_data):
    return None


class _FreshClock:
    """In-process calls see the pytest process's age; pin 'time since start' to 0 for them."""

    def setUp(self):
        p = mock.patch.object(fc, "elapsed_since_process_start", return_value=0.0)
        p.start()
        self.addCleanup(p.stop)


class RunnerVerdicts(_FreshClock, unittest.TestCase):
    def test_allow_is_silent_exit_0(self):
        self.assertEqual(fc.evaluate("h", _allow, stdin=_payload()), (0, "", "", False))

    def test_rule_deny_passes_through_as_json_exit_0(self):
        deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "no"}}
        code, out, err, _ = fc.evaluate("h", lambda d: deny, stdin=_payload())
        self.assertEqual((code, json.loads(out), err), (0, deny, ""))

    def test_empty_stdin_is_the_old_empty_payload(self):
        seen = []
        code, *_ = fc.evaluate("h", lambda d: seen.append(d), stdin=io.StringIO("  \n"))
        self.assertEqual((code, seen), (0, [{}]))

    def _denied(self, cause, decide=_allow, stdin=None, **kw):
        code, out, err, _ = fc.evaluate("my-hook", decide, stdin=stdin or _payload(), **kw)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith(f"BLOCKED (my-hook, fail-closed, T-4392): {cause}: "), err)
        self.assertEqual(err.count("\n"), 1)
        return err

    def test_exception_denies(self):
        def boom(d):
            raise KeyError("x")
        err = self._denied("exception", boom)
        self.assertIn("KeyError", err)
        # self-diagnosing in CI: the innermost frame is named
        self.assertRegex(err, r"\(at test_failclosed\.py:\d+ in boom\)")

    def test_systemexit_and_baseexception_deny(self):
        def leave(d):
            sys.exit(0)
        self._denied("exception", leave)

        def interrupted(d):
            raise KeyboardInterrupt
        self._denied("exception", interrupted)

    def test_ambiguity_denies(self):
        self._denied("ambiguity", stdin=io.StringIO("{not json"))
        self._denied("ambiguity", stdin=io.StringIO("[1, 2]"))
        self._denied("ambiguity", stdin=io.StringIO('{"tool_name":"Bash","tool_input":"ls"}'))
        self._denied("ambiguity", stdin=io.StringIO('{"tool_name":"Bash","tool_input":{"command":5}}'))
        self._denied("ambiguity", decide=lambda d: "deny")

        def unsure(d):
            raise fc.Ambiguous("two readings")
        self.assertIn("two readings", self._denied("ambiguity", unsure))

    def test_cap_denies_before_decide_runs(self):
        called = []
        self._denied("cap", lambda d: called.append(1), stdin=_payload("a" * 101), max_command_chars=100)
        self.assertEqual(called, [])
        code, *_ = fc.evaluate("h", _allow, stdin=_payload("a" * 100), max_command_chars=100)
        self.assertEqual(code, 0)

    def test_cap_only_applies_to_bash_command(self):
        code, *_ = fc.evaluate("h", _allow, stdin=_payload("a" * 500, tool="Write"), max_command_chars=100)
        self.assertEqual(code, 0)

    def test_default_cap(self):
        self.assertEqual(fc.DEFAULT_MAX_COMMAND_CHARS, 65_536)


class Deadline(_FreshClock, unittest.TestCase):
    def _timed_deny(self, decide, stdin=None, deadline=0.3):
        t0 = time.monotonic()
        code, out, err, _ = fc.evaluate("h", decide, stdin=stdin or _payload(), deadline_s=deadline)
        took = time.monotonic() - t0
        self.assertEqual(code, 2, err)
        self.assertIn(": deadline: ", err)
        # A deadline deny is TRANSIENT (orchestrator's ruling, T-4392 2d): the remedy says retry first,
        # split only on a repeat, and break-glass only for a persistent failure.
        self.assertIn("Retry once the load eases (a transient stall is not a lockout); if it "
                      "repeats on the same command, split or simplify it. Break-glass is only "
                      "for a persistent hook failure.", err)
        # self-diagnosing in CI: how much was spent before the body, and at the deny
        self.assertRegex(err, r"\(\d+\.\d\ds had elapsed before the hook body started, "
                              r"\d+\.\d\ds at the deny\)")
        self.assertLess(took, deadline + 2.0)
        return err

    def test_slow_decide_denies(self):
        self._timed_deny(lambda d: time.sleep(10))

    def test_catastrophic_regex_is_interrupted(self):
        self._timed_deny(lambda d: re.match(r"(a+)+$", "a" * 40 + "b"))

    def test_blocked_stdin_denies(self):
        r, w = os.pipe()
        try:
            with os.fdopen(r, "r") as reader:
                self._timed_deny(_allow, stdin=reader)
        finally:
            os.close(w)

    def test_budget_already_spent_denies_immediately(self):
        called = []
        with mock.patch.object(fc, "elapsed_since_process_start", return_value=fc.DEADLINE_S + 1):
            code, _, err, _ = fc.evaluate("h", lambda d: called.append(1), stdin=_payload())
        self.assertEqual((code, called), (2, []))
        self.assertIn("already spent", err)

    def test_thread_fallback_denies_off_main_thread(self):
        box = {}
        t = threading.Thread(target=lambda: box.setdefault(
            "r", fc.evaluate("h", lambda d: time.sleep(10), stdin=_payload(), deadline_s=0.3)))
        t.start()
        t.join(5)
        code, _, err, abandoned = box["r"]
        self.assertEqual((code, abandoned), (2, True))
        self.assertIn(": deadline: ", err)

    def test_thread_fallback_propagates_exceptions_as_deny(self):
        box = {}

        def boom(d):
            raise ValueError("x")
        t = threading.Thread(target=lambda: box.setdefault("r", fc.evaluate("h", boom, stdin=_payload())))
        t.start()
        t.join(5)
        self.assertEqual(box["r"][0], 2)
        self.assertIn(": exception: ValueError", box["r"][2])

    def test_deadline_override_can_only_shorten(self):
        for bad in (0, -1, math.nan, math.inf, fc.DEADLINE_S + 1, None):
            self.assertEqual(fc._clamp_deadline(bad), fc.DEADLINE_S, bad)
        self.assertEqual(fc._clamp_deadline(0.5), 0.5)

    def test_alarm_disarmed_and_handler_restored(self):
        import signal
        before = signal.getsignal(signal.SIGALRM)
        fc.evaluate("h", _allow, stdin=_payload())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertIs(signal.getsignal(signal.SIGALRM), before)


class BudgetInvariant(unittest.TestCase):
    def test_deadline_leaves_margin_under_smallest_wired_timeout(self):
        # Claude Code hook timeouts are SECONDS; the wrapped hooks go to 30 after this lands.
        self.assertEqual(fc.MIN_WIRED_TIMEOUT_S, 30.0)
        self.assertEqual(fc.DEADLINE_S, 15.0)  # raised from 5s on the measured load distribution
        self.assertLessEqual(fc.DEADLINE_S + fc.DEADLINE_MARGIN_S, fc.MIN_WIRED_TIMEOUT_S)
        self.assertGreaterEqual(fc.DEADLINE_MARGIN_S, 5.0)

    def test_process_start_clock_counts_from_spawn(self):
        # A real subprocess that sleeps 0.4s before asking: elapsed must include the sleep.
        code = ("import sys,time; sys.path.insert(0, %r); time.sleep(0.4); import _failclosed as f; "
                "print(f.elapsed_since_process_start())" % str(_HOOKS))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertGreaterEqual(float(out.stdout), 0.4)


class RealProcess(unittest.TestCase):
    """The real run() in a real process: exit codes, one stream each, the real start clock."""

    def _run(self, body, stdin='{"tool_name":"Bash","tool_input":{"command":"ls"}}'):
        code = "import sys,time; sys.path.insert(0, %r); import _failclosed as f\n" % str(_HOOKS)
        return subprocess.run([sys.executable, "-c", code + textwrap.dedent(body)], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def test_real_deadline_exit_2(self):
        r = self._run("f.run('h', lambda d: time.sleep(20), deadline_s=0.5)")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("BLOCKED (h, fail-closed, T-4392): deadline", r.stderr)

    def test_real_spent_budget_exit_2(self):
        r = self._run("time.sleep(0.6); f.run('h', lambda d: None, deadline_s=0.5)")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("already spent", r.stderr)

    def test_real_allow_exit_0(self):
        r = self._run("f.run('h', lambda d: None)")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


# ---------------------------------------------------------------------------------------------
# break-glass fixtures
# ---------------------------------------------------------------------------------------------
class _Tree:
    """base/ACCOUNT/flag under a temp dir, modes set explicitly (umask-proof)."""

    def __init__(self, tc, account="acct"):
        self._td = tempfile.TemporaryDirectory()
        tc.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.base = self.root / "base"
        self.account = account
        self.acct = self.base / account
        self.flag = self.acct / fc.FLAG_NAME
        self.acct.mkdir(parents=True)
        os.chmod(self.base, 0o755)
        os.chmod(self.acct, 0o755)

    def write(self, content: bytes | str, mode=0o644):
        if isinstance(content, str):
            content = content.encode()
        self.flag.write_bytes(content)
        os.chmod(self.flag, mode)

    def valid(self, hooks="mcp-list-guard", hours=2.0, now=None):
        now = time.time() if now is None else now
        self.write(f"v1 {int(now + hours * 3600)} {hooks}\n")

    def accept_policy(self, **over):
        """The injected owner policy under which this tree's own files are acceptable."""
        pol = dict(account=self.account, base=str(self.base), root_uid=_ME,
                   hook_uids={_OTHER}, allowed_owner_uids={_ME})
        pol.update(over)
        return pol


def _inspect(tree, **over):
    pol = tree.accept_policy(**over)
    pol.pop("account")
    return fc.inspect_flag(tree.account, **pol)


class BreakGlassAccept(unittest.TestCase):
    def test_valid_flag_is_live(self):
        t = _Tree(self)
        t.valid()
        s = _inspect(t)
        self.assertTrue(s.live, s.reason)
        self.assertEqual(s.hooks, ("mcp-list-guard",))
        self.assertEqual(s.owner_uid, _ME)

    def test_reason_line_accepted(self):
        t = _Tree(self)
        t.write(f"v1 {int(time.time()) + 3600} *\nreason: poller probe wedged, T-9999\n")
        s = _inspect(t)
        self.assertTrue(s.live, s.reason)
        self.assertEqual(s.note, "poller probe wedged, T-9999")

    def test_breakglass_for_matches_hook_or_star(self):
        t = _Tree(self)
        t.valid("mcp-list-guard,background-pipe-gate")
        self.assertIsNotNone(fc.breakglass_for("mcp-list-guard", t.accept_policy()))
        self.assertIsNone(fc.breakglass_for("continuity-candidate-guard", t.accept_policy()))
        t.valid("*")
        self.assertIsNotNone(fc.breakglass_for("continuity-candidate-guard", t.accept_policy()))

    def test_worker_guard_is_excluded_even_under_star(self):
        t = _Tree(self)
        t.valid("*")
        self.assertIsNone(fc.breakglass_for("worker-guard", t.accept_policy()))
        t.valid("worker-guard")
        self.assertIsNone(fc.breakglass_for("worker-guard", t.accept_policy()))

    def test_no_flag_or_no_dir_is_none(self):
        t = _Tree(self)
        self.assertIsNone(_inspect(t))
        self.assertIsNone(fc.inspect_flag("nobody-here", base=str(t.base), root_uid=_ME,
                                          hook_uids={_OTHER}, allowed_owner_uids={_ME}))
        self.assertIsNone(fc.inspect_flag("x", base=str(t.root / "absent")))


class BreakGlassRejectRealOwnership(unittest.TestCase):
    """Rejections driven by the REAL uids of the files, with no injected owner policy."""

    @unittest.skipIf(os.geteuid() == 0, "as root the fixture base IS root-owned")
    def test_base_not_root_owned_is_rejected(self):
        t = _Tree(self)
        t.valid()
        s = fc.inspect_flag(t.account, base=str(t.base))
        self.assertFalse(s.live)
        self.assertIn("root-owned", s.reason)

    def test_flag_owned_by_hook_uid_is_rejected(self):
        # Treat the test user's base as "root-owned" (root_uid injected) but keep the REAL hook
        # uids (this process's account): the test user's own flag must be refused as same-uid.
        me = fc.current_account()
        t = _Tree(self, account=me)
        t.valid()
        s = fc.inspect_flag(me, base=str(t.base), root_uid=_ME)
        self.assertFalse(s.live)
        self.assertEqual(s.reason, "flag is owned by the hook's own uid")
        self.assertIsNone(fc.breakglass_for("mcp-list-guard", {"account": me, "base": str(t.base),
                                                                "root_uid": _ME}))

    def test_injected_allowlist_cannot_readmit_the_hook_uid(self):
        # Even if a caller lists the hook's own uid as allowed, it is subtracted.
        me = fc.current_account()
        t = _Tree(self, account=me)
        t.valid()
        s = fc.inspect_flag(me, base=str(t.base), root_uid=_ME, allowed_owner_uids={_ME})
        self.assertFalse(s.live)
        self.assertEqual(s.reason, "flag is owned by the hook's own uid")

    def test_no_flag_under_an_untrusted_base_is_silent(self):
        t = _Tree(self)
        self.assertIsNone(fc.inspect_flag(t.account, base=str(t.base)))


class BreakGlassRejectShapes(unittest.TestCase):
    def assertRejected(self, tree, fragment, **over):
        s = _inspect(tree, **over)
        self.assertIsNotNone(s)
        self.assertFalse(s.live)
        self.assertIn(fragment, s.reason)
        self.assertIsNone(fc.breakglass_for("mcp-list-guard", tree.accept_policy(**over)))

    def test_base_group_or_world_writable(self):
        for mode in (0o775, 0o757):
            t = _Tree(self)
            t.valid()
            os.chmod(t.base, mode)
            self.assertRejected(t, "group/world-writable")

    def test_account_dir_group_or_world_writable(self):
        for mode in (0o775, 0o757):
            t = _Tree(self)
            t.valid()
            os.chmod(t.acct, mode)
            self.assertRejected(t, "account dir is group/world-writable")

    def test_account_dir_owner_not_permitted(self):
        # One test user owns every fixture file, so the account dir's owner is re-reported as a
        # third uid (fstat wrapped for that one fd); base and flag keep their real stats.
        t = _Tree(self)
        t.valid()
        real_fstat = os.fstat
        acct_ino = os.stat(t.acct).st_ino

        def fstat(fd):
            st = real_fstat(fd)
            if st.st_ino == acct_ino:
                fields = list(st)
                fields[4] = _OTHER + 7  # st_uid
                return os.stat_result(fields)
            return st
        with mock.patch.object(fc.os, "fstat", side_effect=fstat):
            self.assertRejected(t, "not a permitted creator")
            # ... and the SAME uid is accepted once it is an allowed creator.
            s = _inspect(t, allowed_owner_uids={_ME, _OTHER + 7})
            self.assertTrue(s.live, s.reason)

    def test_symlinked_base(self):
        t = _Tree(self)
        t.valid()
        link = t.root / "link"
        link.symlink_to(t.base)
        s = fc.inspect_flag(t.account, base=str(link), root_uid=_ME, hook_uids={_OTHER},
                            allowed_owner_uids={_ME})
        self.assertFalse(s.live)
        self.assertIn("without symlinks", s.reason)

    def test_symlinked_ancestor(self):
        t = _Tree(self)
        t.valid()
        (t.root / "alias").symlink_to(t.root)
        s = fc.inspect_flag(t.account, base=str(t.root / "alias" / "base"), root_uid=_ME,
                            hook_uids={_OTHER}, allowed_owner_uids={_ME})
        self.assertFalse(s.live)

    def test_symlinked_account_dir(self):
        t = _Tree(self)
        real = t.base / "real"
        real.mkdir(mode=0o755)
        (real / fc.FLAG_NAME).write_text(f"v1 {int(time.time()) + 3600} *\n")
        os.chmod(real / fc.FLAG_NAME, 0o644)
        os.rmdir(t.acct)
        t.acct.symlink_to(real)
        self.assertRejected(t, "account dir unusable")

    def test_symlinked_flag(self):
        t = _Tree(self)
        target = t.root / "elsewhere"
        target.write_text(f"v1 {int(time.time()) + 3600} *\n")
        t.flag.symlink_to(target)
        self.assertRejected(t, "not openable as a plain file")

    def test_fifo_does_not_hang(self):
        t = _Tree(self)
        os.mkfifo(t.flag, 0o644)
        t0 = time.monotonic()
        self.assertRejected(t, "not a regular file")
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_directory_named_flag(self):
        t = _Tree(self)
        t.flag.mkdir(mode=0o755)
        self.assertRejected(t, "not a regular file")

    def test_flag_group_or_world_writable(self):
        for mode in (0o664, 0o646):
            t = _Tree(self)
            t.valid()
            os.chmod(t.flag, mode)
            self.assertRejected(t, "flag is group/world-writable")

    def test_hardlinked_flag(self):
        t = _Tree(self)
        t.valid()
        os.link(t.flag, t.acct / "twin")
        self.assertRejected(t, "nlink 2")

    def test_owner_not_in_allowlist(self):
        t = _Tree(self)
        t.valid()
        self.assertRejected(t, "not in the allowlist", allowed_owner_uids={_OTHER + 1})

    def test_expired(self):
        t = _Tree(self)
        t.valid(hours=-0.01)
        self.assertRejected(t, "expired")

    def test_more_than_24h_out(self):
        t = _Tree(self)
        t.valid(hours=24.1)
        self.assertRejected(t, "more than 24h out")

    def test_expiry_beyond_ctime_plus_24h(self):
        # Written now for now+25h, then read 2h later: within 24h of "now", but a dormant flag.
        t = _Tree(self)
        real_now = time.time()
        t.valid(hours=25, now=real_now)
        self.assertRejected(t, "after its ctime", now=real_now + 2 * 3600)

    def test_malformed_contents(self):
        exp = int(time.time()) + 3600
        bad = [
            b"", b"\n", f"v1 {exp} mcp-list-guard".encode(),           # no newline
            f"v2 {exp} mcp-list-guard\n".encode(),
            f"v1  {exp} mcp-list-guard\n".encode(),
            f" v1 {exp} mcp-list-guard\n".encode(),
            f"v1 {exp} mcp-list-guard \n".encode(),
            f"v1 {exp} mcp-list-guard\r\n".encode(),
            f"v1 {exp}.5 mcp-list-guard\n".encode(),
            f"v1 0{exp} mcp-list-guard\n".encode(),
            f"v1 -{exp} mcp-list-guard\n".encode(),
            f"v1 {exp} \n".encode(),
            f"v1 {exp} mcp-list-guard,\n".encode(),
            f"v1 {exp} MCP-LIST-GUARD\n".encode(),
            f"v1 {exp} mcp-list-guard.py\n".encode(),
            f"v1 {exp} *,mcp-list-guard\n".encode(),
            f"v1 {exp} mcp-list-guard\nextra\n".encode(),
            f"v1 {exp} mcp-list-guard\nreason: \n".encode(),
            f"v1 {exp} mcp-list-guard\nreason: café\n".encode(),
            f"v1 {exp} mcp-list-guard\nreason: {'x' * 201}\n".encode(),
            f"v1 {exp} mcp-list-guard\n".encode() + b"\x00",
            b"\xef\xbb\xbf" + f"v1 {exp} mcp-list-guard\n".encode(),
            f"v1 {exp} {','.join(['a'] * 400)}\n".encode(),              # over MAX_FLAG_BYTES
        ]
        for content in bad:
            with self.subTest(content=content[:60]):
                t = _Tree(self)
                t.write(content)
                self.assertRejected(t, "malformed")

    def test_parse_flag_strict_accepts_only_the_grammar(self):
        self.assertEqual(fc.parse_flag(b"v1 1790000000 *\n"), (1790000000, ("*",), None))
        self.assertEqual(fc.parse_flag(b"v1 1790000000 a,b-c,d_e\nreason: why\n"),
                         (1790000000, ("a", "b-c", "d_e"), "why"))


class BreakGlassInTheRunner(_FreshClock, unittest.TestCase):
    def test_fast_path_never_reads_breakglass(self):
        deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
        with mock.patch.object(fc, "breakglass_for", side_effect=AssertionError("read!")) as bg, \
                mock.patch.object(fc, "inspect_flag", side_effect=AssertionError("read!")):
            self.assertEqual(fc.evaluate("h", _allow, stdin=_payload())[0], 0)
            self.assertEqual(fc.evaluate("h", lambda d: deny, stdin=_payload())[0], 0)
        bg.assert_not_called()

    def test_fail_closed_path_does_read_breakglass(self):
        with mock.patch.object(fc, "breakglass_for", return_value=None) as bg:
            self.assertEqual(fc.evaluate("h", lambda d: 1 / 0, stdin=_payload())[0], 2)
        bg.assert_called_once()

    def test_honoured_flag_turns_each_cause_into_loud_exit_0(self):
        t = _Tree(self)
        t.valid("h")
        pol = t.accept_policy()

        def boom(d):
            raise RuntimeError("x")
        cases = [
            dict(decide=boom),
            dict(decide=_allow, stdin=io.StringIO("{bad")),
            dict(decide=_allow, stdin=_payload("a" * 11), max_command_chars=10),
            dict(decide=lambda d: time.sleep(10), deadline_s=0.3),
        ]
        for kw in cases:
            decide = kw.pop("decide")
            stdin = kw.pop("stdin", None) or _payload()
            code, out, err, _ = fc.evaluate("h", decide, stdin=stdin, _bg_policy=pol, **kw)
            self.assertEqual(code, 0, err)
            self.assertTrue(err.startswith("BREAK-GLASS ACTIVE for h on acct: would have DENIED ("), err)
            msg = json.loads(out)
            self.assertEqual(list(msg), ["systemMessage"])  # NO permissionDecision
            self.assertEqual(msg["systemMessage"] + "\n", err)

    def test_rule_deny_still_denies_while_honoured(self):
        t = _Tree(self)
        t.valid("*")
        deny = {"hookSpecificOutput": {"permissionDecision": "deny"}}
        code, out, err, _ = fc.evaluate("h", lambda d: deny, stdin=_payload(), _bg_policy=t.accept_policy())
        self.assertEqual((code, json.loads(out), err), (0, deny, ""))

    def test_worker_guard_never_honoured(self):
        t = _Tree(self)
        t.valid("*")
        with mock.patch.object(fc, "breakglass_for", side_effect=AssertionError("read!")):
            code, *_ = fc.evaluate("worker-guard", lambda d: 1 / 0, stdin=_payload(),
                                   _bg_policy=t.accept_policy())
        self.assertEqual(code, 2)

    def test_breakglass_check_error_or_hang_means_deny(self):
        with mock.patch.object(fc, "breakglass_for", side_effect=OSError("io")):
            self.assertEqual(fc.evaluate("h", lambda d: 1 / 0, stdin=_payload())[0], 2)
        with mock.patch.object(fc, "_BREAKGLASS_CHECK_S", 0.2), \
                mock.patch.object(fc, "breakglass_for", side_effect=lambda *a: time.sleep(10)):
            t0 = time.monotonic()
            self.assertEqual(fc.evaluate("h", lambda d: 1 / 0, stdin=_payload())[0], 2)
            self.assertLess(time.monotonic() - t0, 3)


class Allowlists(unittest.TestCase):
    def _uid(self, name):
        return fc._uid_of(name)

    def test_table(self):
        seat = {"claude", "claude-research", "suborch1", "suborch2", "exocortex"}
        for acct in seat:
            self.assertEqual(fc.ACCOUNT_ALLOWLISTS[acct], ("root", "claude-fallback"), acct)
        self.assertEqual(fc.ACCOUNT_ALLOWLISTS["claude-fallback"], ("root",))
        for names in fc.ACCOUNT_ALLOWLISTS.values():
            self.assertNotIn("claude", names)
        self.assertEqual(fc.DEFAULT_ALLOWLIST, ("root",))

    def test_resolution(self):
        cf = self._uid("claude-fallback")
        self.assertEqual(fc.allowlist_uids("claude-fallback", set()), frozenset({0}))
        self.assertEqual(fc.allowlist_uids("not-an-account", set()), frozenset({0}))
        if cf is not None:
            self.assertEqual(fc.allowlist_uids("suborch1", set()), frozenset({0, cf}))
            self.assertEqual(fc.allowlist_uids("suborch1", {cf}), frozenset({0}))  # hook uid dropped

    def test_claude_uid_never_allowed_even_if_listed(self):
        claude = self._uid("claude")
        if claude is None:
            self.skipTest("no claude account on this host")
        with mock.patch.dict(fc.ACCOUNT_ALLOWLISTS, {"x": ("root", "claude")}):
            self.assertNotIn(claude, fc.allowlist_uids("x", set()))

    def test_default_hook_uids_include_this_process(self):
        me = fc.current_account()
        self.assertIn(os.geteuid(), fc._default_hook_uids(me))


class ListingCli(unittest.TestCase):
    def _cli(self, *args):
        return subprocess.run([sys.executable, str(_HOOKS / "_failclosed.py"), "--list", *args],
                              capture_output=True, text=True, timeout=30)

    @unittest.skipIf(os.geteuid() == 0, "as root the fixture base IS root-owned")
    def test_rejected_flag_is_listed(self):
        t = _Tree(self)
        t.valid()
        r = self._cli("--base", str(t.base), "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^REJECTED acct: base is not root-owned")

    def test_no_flag_is_silent(self):
        t = _Tree(self)
        r = self._cli("--base", str(t.base), "--account", "acct")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = self._cli("--base", str(t.root / "absent"), "--all")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_live_format(self):
        s = fc.FlagStatus("acct", live=True, owner_name="root", expiry=10_000 + 3 * 3600 + 120,
                          hooks=("mcp-list-guard",), note="why")
        self.assertEqual(fc.format_status(s, now=10_000),
                         "LIVE acct: hooks=mcp-list-guard owner=root expires=1970-01-01T05:48:40Z "
                         "(in 3h02m) reason='why'")


if __name__ == "__main__":
    unittest.main()
