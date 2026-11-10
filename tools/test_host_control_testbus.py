"""Tests for the shared private-dbus-daemon spawn/teardown/sweep helper
(tools/_host_control_testbus.py, T-2932).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tools._host_control_testbus as testbus

REPO_ROOT = Path(__file__).resolve().parent.parent

requires_private_bus = unittest.skipUnless(
    testbus.DBUS_DAEMON, "testbus tests need dbus-daemon"
)

_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_if_alive(pid: int, sig: int = signal.SIGKILL) -> None:
    if _pid_alive(pid):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _cleanup_popen(proc: subprocess.Popen) -> None:
    """Kill (if still running), reap, and close pipes -- avoids the
    ResourceWarning noise a bare os.kill()-by-pid leaves behind when we
    still hold the Popen object."""

    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout is not None:
        proc.stdout.close()


@requires_private_bus
class TestbusHelperTests(unittest.TestCase):
    """Direct tests of the shared spawn/teardown/sweep helper -- not the
    host_control service, just the process-lifecycle plumbing.

    Hermetic by construction (parallel/xdist-safe): every test gets its
    own private root holding (a) the tempdir that ``_short_tmpdir`` /
    ``spawn_daemon`` create bus dirs under (``tempfile.tempdir`` in this
    process, ``TMPDIR`` for spawned children), (b) its own
    ``XDG_RUNTIME_DIR`` so the pidfile dir that ``sweep()`` scans and the
    symlink-squat tests replace is never the shared
    ``$XDG_RUNTIME_DIR/pantheon-testbus`` another process is using, and
    (c) its own no-XDG fallback root. Every "nothing leaked" check is
    scoped to that root, so a sibling test process can never be seen,
    swept or killed."""

    def setUp(self) -> None:
        # Short, unique root directly under /tmp so bus.sock paths stay
        # under the AF_UNIX limit even when $TMPDIR is long.
        self._root = Path(tempfile.mkdtemp(prefix="t2932root-", dir="/tmp"))
        # Registered first => runs last, after every per-test cleanup
        # (daemon kills, symlink removal) has run.
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        self._tmp_root = self._root / "tmp"
        self._tmp_root.mkdir(mode=0o700)
        runtime_dir = self._root / "run"
        runtime_dir.mkdir(mode=0o700)
        # Stands in for /tmp itself, which always exists.
        (self._root / "fallback").mkdir(mode=0o700)

        for patcher in (
            mock.patch.object(tempfile, "tempdir", str(self._tmp_root)),
            mock.patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": str(runtime_dir), "TMPDIR": str(self._tmp_root)},
            ),
            mock.patch.object(testbus, "_FALLBACK_BASE_ROOT", self._root / "fallback"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _live_marked_daemon_pids(self) -> set[int]:
        """Live dbus-daemon-referencing processes this test created:
        either the cmdline references THIS test's root, or the process is
        a direct child of this test process (so a regressed spawn that
        escaped the per-test tempdir is still caught). Never matches a
        sibling process's daemon, so the defensive kill in the
        spawn-refused tests below cannot touch another test's processes."""

        needle = (str(self._root) + os.sep).encode()
        own_pid = os.getpid()
        pids: set[int] = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                if b"dbus-daemon" not in cmdline:
                    continue
                # proc(5): ppid is field 4, i.e. index 1 after the last ')'.
                stat_fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                ppid = int(stat_fields[1])
            except (OSError, ValueError, IndexError):
                continue
            if needle in cmdline or ppid == own_pid:
                pids.add(int(entry.name))
        return pids

    def _spawn_orphan_daemon(self) -> tuple[int, str, int, int]:
        """Spawn a dbus-daemon via a throwaway python parent that does
        NOT apply PDEATHSIG, then let that parent exit -- the daemon
        survives, reparented, exactly like a killed-session leak. Returns
        (daemon_pid, bus_dir, parent_pid, parent_start_ticks)."""

        bus_dir = testbus._short_tmpdir("t2932test-")
        socket_path = os.path.join(bus_dir, "bus.sock")
        bus_config = os.path.join(bus_dir, "bus.conf")
        with open(bus_config, "w", encoding="utf-8") as handle:
            handle.write(_BUS_CONFIG.format(socket=socket_path))
        script = (
            "import subprocess, sys, warnings\n"
            # This parent must exit while its Popen child is still alive: that
            # is the orphan state sweep() is meant to repair.  Force warnings
            # on for the fixture, then suppress only Popen.__del__'s expected
            # complaint about that deliberate lifecycle violation.
            "warnings.filterwarnings('ignore', category=ResourceWarning, "
            "module='subprocess')\n"
            f"p = subprocess.Popen([{testbus.DBUS_DAEMON!r}, "
            f"{f'--config-file={bus_config}'!r}, '--nofork'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(p.pid)\n"
            "sys.stdout.flush()\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-W", "always::ResourceWarning", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = parent.stdout.readline()
        daemon_pid = int(line.strip())
        parent_start_ticks = testbus._start_ticks(parent.pid)
        parent.wait(timeout=10)  # parent is now genuinely, fully gone
        parent_stderr = parent.stderr.read()
        parent.stdout.close()
        parent.stderr.close()
        if "ResourceWarning" in parent_stderr:
            raise AssertionError(
                "intentional orphan fixture emitted a ResourceWarning:\n"
                f"{parent_stderr}"
            )
        deadline = time.monotonic() + 5
        while not os.path.exists(socket_path):
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        return daemon_pid, bus_dir, parent.pid, parent_start_ticks

    def test_pidfile_written_then_removed_around_a_real_spawn(self) -> None:
        handle = testbus.spawn_daemon("t2932test-", _BUS_CONFIG)
        try:
            data = json.loads(handle.pidfile.read_text(encoding="utf-8"))
            self.assertEqual(data["daemon_pid"], handle.process.pid)
            self.assertEqual(data["driver_pids"], [])
            self.assertEqual(data["bus_dir"], handle.bus_dir)
            self.assertEqual(data["parent_pid"], os.getpid())
            self.assertIn("parent_start_ticks", data)
        finally:
            testbus.stop_daemon(handle)
        self.assertFalse(handle.pidfile.exists())
        self.assertFalse(os.path.exists(handle.bus_dir))

    def test_register_driver_appends_and_dedups(self) -> None:
        handle = testbus.spawn_daemon("t2932test-", _BUS_CONFIG)
        self.addCleanup(lambda: testbus.stop_daemon(handle))
        handle.register_driver(111)
        handle.register_driver(222)
        handle.register_driver(111)  # duplicate -- must not double-append
        data = json.loads(handle.pidfile.read_text(encoding="utf-8"))
        self.assertEqual(data["driver_pids"], [111, 222])

    def test_sweep_reaps_daemon_orphaned_by_an_already_reaped_parent(self) -> None:
        daemon_pid, bus_dir, parent_pid, start_ticks = self._spawn_orphan_daemon()
        self.addCleanup(lambda: _kill_if_alive(daemon_pid))
        pidfile_dir = testbus._pidfile_dir_for_spawn()
        pidfile = pidfile_dir / f"{daemon_pid}.json"
        testbus._atomic_write_json(
            pidfile,
            {
                "daemon_pid": daemon_pid,
                "driver_pids": [],
                "bus_dir": bus_dir,
                "parent_pid": parent_pid,
                "parent_start_ticks": start_ticks,
            },
        )
        self.assertTrue(_pid_alive(daemon_pid), "orphan setup did not survive to the sweep call")

        testbus.sweep()

        deadline = time.monotonic() + 2
        while _pid_alive(daemon_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_pid_alive(daemon_pid), "sweep() did not reap the orphaned daemon")
        self.assertFalse(os.path.exists(bus_dir))
        self.assertFalse(pidfile.exists())

    def test_sweep_skips_a_live_matching_parent(self) -> None:
        handle = testbus.spawn_daemon("t2932test-", _BUS_CONFIG)
        self.addCleanup(lambda: testbus.stop_daemon(handle))

        testbus.sweep()  # our own live daemon; must be a no-op

        self.assertIsNone(handle.process.poll())
        self.assertTrue(handle.pidfile.exists())
        self.assertTrue(os.path.exists(handle.bus_dir))

    def test_sweep_skips_a_reused_pid_cmdline_mismatch(self) -> None:
        # Stand-in for "the daemon pid we recorded got reused by an
        # unrelated process after the daemon actually exited" -- a real
        # process (not a dbus-daemon) at a pid sweep is told to reap.
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: _cleanup_popen(sleeper))
        pidfile_dir = testbus._pidfile_dir_for_spawn()
        pidfile = pidfile_dir / f"{sleeper.pid}-reuse-test.json"
        testbus._atomic_write_json(
            pidfile,
            {
                "daemon_pid": sleeper.pid,
                "driver_pids": [],
                "bus_dir": "",
                "parent_pid": 1,  # init/systemd -- always exists
                "parent_start_ticks": -1,  # sentinel: real starttimes are always >= 0
            },
        )
        self.addCleanup(lambda: pidfile.unlink(missing_ok=True))

        testbus.sweep()

        self.assertTrue(
            _pid_alive(sleeper.pid),
            "sweep() killed a pid whose cmdline did not match -- must never kill a reused pid",
        )

    def test_pdeathsig_kills_daemon_within_2s_of_parent_sigkill(self) -> None:
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from tools._host_control_testbus import spawn_daemon\n"
            f"handle = spawn_daemon('t2932test-', {_BUS_CONFIG!r})\n"
            "print(handle.process.pid)\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        parent = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        line = parent.stdout.readline()
        daemon_pid = int(line.strip())
        self.addCleanup(lambda: _kill_if_alive(daemon_pid))
        self.addCleanup(lambda: _cleanup_popen(parent))
        self.assertTrue(_pid_alive(daemon_pid))

        parent.send_signal(signal.SIGKILL)

        deadline = time.monotonic() + 2
        while _pid_alive(daemon_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(
            _pid_alive(daemon_pid),
            "PDEATHSIG did not reap the daemon within 2s of parent SIGKILL",
        )

    # -- round-2 regression coverage: sweep() must never raise, even when
    # the pidfile dir itself is refused; spawn-time must still refuse loud.

    def _force_base_dir(self, *, use_xdg: bool) -> Path:
        """Return the pidfile base dir testbus would compute right now,
        forcing either the XDG branch or the /tmp fallback branch (via a
        temporary os.environ patch, restored in cleanup)."""

        if use_xdg:
            base = testbus._pidfile_base_dir()
            # setUp always injects a per-test XDG_RUNTIME_DIR, so the XDG
            # branch is exercisable on every runner, session or not.
            self.assertEqual(base, Path(os.environ["XDG_RUNTIME_DIR"]) / "pantheon-testbus")
            return base

        original = os.environ.pop("XDG_RUNTIME_DIR", None)

        def _restore() -> None:
            if original is not None:
                os.environ["XDG_RUNTIME_DIR"] = original

        self.addCleanup(_restore)
        base = testbus._pidfile_base_dir()
        self.assertEqual(base, self._root / "fallback" / f"pantheon-testbus-{os.getuid()}")
        return base

    def _squat_with_symlink(self, base: Path) -> None:
        """Replace `base` with a symlink -- stands in for a hostile/
        pre-existing entry on a shared, sticky, world-writable /tmp."""

        if base.is_symlink():
            base.unlink()
        elif base.is_dir():
            shutil.rmtree(base)
        elif base.exists():
            base.unlink()
        target = testbus._short_tmpdir("t2932test-squat-")
        base.symlink_to(target)

        def _cleanup() -> None:
            if base.is_symlink():
                base.unlink()
            shutil.rmtree(target, ignore_errors=True)

        self.addCleanup(_cleanup)

    def test_sweep_never_raises_when_base_dir_is_a_symlink(self) -> None:
        # covers both the XDG_RUNTIME_DIR branch and the /tmp fallback --
        # the ownership check must run (and sweep must survive it) either
        # way the base dir gets resolved.
        for use_xdg in (True, False):
            with self.subTest(use_xdg=use_xdg):
                base = self._force_base_dir(use_xdg=use_xdg)
                self._squat_with_symlink(base)

                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    testbus.sweep()  # must NOT raise

                lines = [line for line in buf.getvalue().splitlines() if line]
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("PidfileDirRefused", lines[0])
                self.assertIn(str(base), lines[0])

    def test_sweep_never_raises_when_ownership_check_itself_raises(self) -> None:
        # stands in for "dir owned by another uid" -- not directly
        # testable without root, so we monkeypatch the check itself to
        # raise the same exception type a wrong-uid dir would produce.
        testbus._pidfile_dir_for_spawn()  # ensure the real dir exists first
        original = testbus._verify_owned_dir

        def _always_refuse(path: Path) -> None:
            raise testbus.PidfileDirRefused(f"stand-in for a wrong-uid dir: {path}")

        testbus._verify_owned_dir = _always_refuse
        self.addCleanup(lambda: setattr(testbus, "_verify_owned_dir", original))

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            testbus.sweep()  # must NOT raise

        lines = [line for line in buf.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("PidfileDirRefused", lines[0])

    def test_spawn_daemon_raises_when_base_dir_is_a_symlink(self) -> None:
        base = self._force_base_dir(use_xdg=True)
        self._squat_with_symlink(base)

        before = self._live_marked_daemon_pids()
        try:
            with self.assertRaises(testbus.PidfileDirRefused):
                testbus.spawn_daemon("t2932test-", _BUS_CONFIG)
        finally:
            # Defense in depth: if the validate-before-create ordering
            # (see test_spawn_daemon_leaves_nothing_behind_when_base_dir_
            # is_refused below) ever regresses, don't let THIS test leak
            # a daemon while proving the point.
            for pid in self._live_marked_daemon_pids() - before:
                _kill_if_alive(pid)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_spawn_daemon_leaves_nothing_behind_when_base_dir_is_refused(self) -> None:
        # Round 2b: the pidfile dir is validated BEFORE anything else is
        # created (_short_tmpdir, the log file, the daemon Popen), so a
        # refused dir must raise with NOTHING left behind at all -- no
        # daemon process, no tmpdir. This is the strengthened contract;
        # the test above (round 2) only proved "doesn't crash / any leak
        # gets caught by this test suite's own cleanup," which was the
        # correct bar before this ordering fix landed.
        base = self._force_base_dir(use_xdg=True)
        self._squat_with_symlink(base)

        before_pids = self._live_marked_daemon_pids()
        before_tmpdirs = set(self._tmp_root.glob("t2932test-*"))
        try:
            with self.assertRaises(testbus.PidfileDirRefused):
                testbus.spawn_daemon("t2932test-", _BUS_CONFIG)

            after_pids = self._live_marked_daemon_pids()
            after_tmpdirs = set(self._tmp_root.glob("t2932test-*"))
            self.assertEqual(
                after_pids,
                before_pids,
                "spawn_daemon() must not spawn a daemon at all when the "
                "pidfile dir is refused -- validation happens first now",
            )
            self.assertEqual(
                after_tmpdirs,
                before_tmpdirs,
                "spawn_daemon() must not leave a tmpdir behind when the "
                "pidfile dir is refused",
            )
        finally:
            # Same defense-in-depth as the test above, in case this
            # assertion is what catches a future regression.
            for pid in self._live_marked_daemon_pids() - before_pids:
                _kill_if_alive(pid)
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            for tmpdir in set(self._tmp_root.glob("t2932test-*")) - before_tmpdirs:
                shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
