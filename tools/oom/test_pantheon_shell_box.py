#!/usr/bin/env python3
"""Tests for tools/oom/pantheon-shell-box.sh (T-5222).

The wrapper is a thin shell script, so these run it for real. The boxed cases need a
reachable systemd user manager and are skipped without one (CI); the fail-open and
contract cases run everywhere.
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parent / "pantheon-shell-box.sh"


def _user_manager() -> bool:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    return bool(rt) and Path(rt, "systemd", "private").is_socket()


def run(*args: str, env: dict | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})}, input=stdin)


class ContractTests(unittest.TestCase):
    """claude.exe passes the whole joined command as ONE argument (function lce)."""

    def test_single_argument_runs_as_a_shell_command_string(self):
        r = run("echo 'a b' && printf '%s\\n' \"$((2+3))\"; exit 7")
        self.assertEqual(r.returncode, 7)
        self.assertEqual(r.stdout, "a b\n5\n")

    def test_several_arguments_run_as_argv(self):
        r = run("printf", "%s|", "x y", "z")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "x y|z|")

    def test_stdin_and_stderr_pass_through(self):
        r = run("cat; echo err >&2", stdin="hello\n")
        self.assertEqual(r.stdout, "hello\n")
        self.assertEqual(r.stderr, "err\n")

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(run().returncode, 2)

    def test_the_session_shield_is_not_inherited(self):
        r = run("cat /proc/self/oom_score_adj")
        self.assertEqual(r.stdout.strip(), "0")


def _own_cgroup() -> str:
    return Path("/proc/self/cgroup").read_text(encoding="utf-8").strip()


class FailOpenTests(unittest.TestCase):
    """Unboxed means no NEW scope: the child stays in the test process's own cgroup. Not
    "no run-*.scope at all", which fails when the suite itself runs from a seat shell the
    wrapper already boxed (T-5802)."""

    def test_no_user_manager_runs_the_command_unboxed(self):
        r = run("cat /proc/self/cgroup; exit 3", env={"XDG_RUNTIME_DIR": "/nonexistent"})
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout.strip(), _own_cgroup())

    def test_kill_switch_runs_unboxed(self):
        r = run("cat /proc/self/cgroup", env={"PANTHEON_SHELL_BOX": "off"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), _own_cgroup())


class ScopeStartFailureTests(unittest.TestCase):
    """orchestrator review: a user manager that LOOKS reachable but cannot start the scope must not
    break the command. A stub systemd-run and a fake manager socket make this run anywhere."""

    def setUp(self):
        import socket
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "rt" / "systemd").mkdir(parents=True)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(root / "rt" / "systemd" / "private"))
        self.addCleanup(self.sock.close)
        (root / "bin").mkdir()
        (root / "home").mkdir()
        self.root = root

    def _stub(self, body: str) -> dict:
        stub = self.root / "bin" / "systemd-run"
        stub.write_text("#!/bin/bash\n" + body + "\n")
        stub.chmod(0o755)
        return {"XDG_RUNTIME_DIR": str(self.root / "rt"), "HOME": str(self.root / "home"),
                "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}"}

    def test_a_failed_scope_start_runs_the_command_unboxed_and_logs_it(self):
        env = self._stub('echo "Failed to start transient scope unit" >&2; exit 1')
        r = run("echo ran; exit 4", env=env)
        self.assertEqual(r.returncode, 4)
        self.assertEqual(r.stdout, "ran\n")
        self.assertIn("scope start failed (systemd-run rc 1)", r.stderr)
        log = (self.root / "home" / ".local/state/pantheon/shell-box-fallback.log").read_text()
        self.assertIn("UNBOXED", log)
        self.assertEqual(list((self.root / "rt").glob("pantheon-shell-box.*")), [])

    def test_a_command_that_fails_inside_the_scope_is_not_rerun(self):
        # The stub runs the command the way systemd-run does (exec after "scope start").
        env = self._stub('while [ "$1" != -- ]; do shift; done; shift; exec "$@"')
        counter = self.root / "count"
        r = run(f"echo x >> {counter}; exit 1", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(counter.read_text(), "x\n")  # ran exactly once
        self.assertNotIn("UNBOXED", r.stderr)


@unittest.skipUnless(_user_manager(), "needs a reachable systemd user manager")
class BoxedTests(unittest.TestCase):
    def test_the_command_runs_in_its_own_transient_scope(self):
        r = run("grep -o 'run-[^/]*\\.scope' /proc/self/cgroup")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^run-.*\.scope$")

    def test_a_runaway_is_killed_inside_its_scope_and_the_caller_survives(self):
        # 300 MB against a 64M cap with no swap: the kernel kills it inside the scope.
        r = run("python3 -c 'b=bytearray(300*1024*1024); print(len(b))'",
                env={"PANTHEON_SHELL_BOX_MEMORY_MAX": "64M", "PANTHEON_SHELL_BOX_SWAP_MAX": "0"})
        # SIGKILL: -9 when the killed process is the one we waited on (bash -c execs a lone
        # command), 137 when a shell above it reports it (the Claude Code case).
        self.assertIn(r.returncode, (-9, 137), (r.stdout, r.stderr))
        self.assertNotIn(str(300 * 1024 * 1024), r.stdout)
        self.assertEqual(run("echo alive").stdout, "alive\n")

    def test_the_memory_cap_is_applied(self):
        r = run("cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max",
                env={"PANTHEON_SHELL_BOX_MEMORY_MAX": "96M"})
        self.assertEqual(r.stdout.strip(), str(96 * 1024 * 1024))


if __name__ == "__main__":
    unittest.main()
