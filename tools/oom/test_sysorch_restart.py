#!/usr/bin/env python3
"""Unittest coverage for tools/oom/sysorch_restart.py (T-5226, ORG-104 rule 3).

Locks the properties that make a root-level self-restart safe:
  1. The memory gate holds a restart while the box is still out of memory,
     releases it as soon as memory is clear, gives up (non-zero) after its
     bound, and fails OPEN on unreadable /proc (never blocks a recovery seat
     on a probe fault).
  2. Only system orchestrators (instance.yaml roles head-system-orchestrator /
     system-orchestrator-N) get the drop-ins; no other seat.
  3. The drop-ins carry back-off, a hold Condition, and the gate.
  4. install is dry-run unless --apply; install/check/uninstall round-trip.
"""
import sys
import tempfile
import unittest
from unittest import mock
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace

# Unconditional: tools/ is a package, so under `python3 -m pytest` / `-m unittest`
# __package__ is set and a guarded insert would leave sysorch_restart unimportable.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sysorch_restart as sr

MEM_CLEAR = "MemTotal: 8000000 kB\nMemAvailable: 5000000 kB\nSwapTotal: 4000000 kB\nSwapFree: 3500000 kB\n"
MEM_OOM = "MemTotal: 8000000 kB\nMemAvailable: 300000 kB\nSwapTotal: 4000000 kB\nSwapFree: 0 kB\n"
PSI_CLEAR = "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
PSI_HIGH = "some avg10=80.00 avg60=40.00 avg300=10.00 total=1\nfull avg10=60.00 avg60=30.00 avg300=8.00 total=1\n"

INSTANCE = {
    "roles": {
        "orchestrator": {"persona": "orchestrator", "account": "claude"},
        "head-system-orchestrator": {"persona": "recovery", "account": "claude-fallback"},
        "system-orchestrator-1": {"persona": "watchman", "account": "codex-fallback"},
        "research": {"persona": "specialist", "account": "claude-research"},
    }
}
PW = {"claude-fallback": (1001, "/home/claude-fallback"),
      "codex-fallback": (1011, "/home/codex-fallback")}


def _pw(name):
    uid, home = PW[name]
    return SimpleNamespace(pw_uid=uid, pw_dir=home, pw_gid=uid)


class _Seq:
    """Returns successive snapshots, then repeats the last."""
    def __init__(self, *snaps):
        self.snaps = list(snaps)

    def __call__(self):
        return self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]


class TestGate(unittest.TestCase):
    def _gate(self, reader, max_wait=1.0):
        return sr.gate(reader=reader, max_wait=max_wait, interval=0.01, sleep=lambda s: None,
                       clock=_FakeClock(step=0.01))

    def test_clear_box_passes_immediately(self):
        self.assertEqual(self._gate(lambda: (MEM_CLEAR, PSI_CLEAR)), 0)

    def test_oom_box_holds_then_times_out(self):
        self.assertEqual(self._gate(lambda: (MEM_OOM, PSI_HIGH)), sr.EXIT_TIMEOUT)

    def test_high_pressure_alone_holds(self):
        self.assertEqual(self._gate(lambda: (MEM_CLEAR, PSI_HIGH)), sr.EXIT_TIMEOUT)

    def test_recovers_mid_wait(self):
        seq = _Seq((MEM_OOM, PSI_HIGH), (MEM_OOM, PSI_HIGH), (MEM_CLEAR, PSI_CLEAR))
        self.assertEqual(self._gate(seq), 0)

    def test_unreadable_proc_fails_open(self):
        def boom():
            raise OSError("no /proc")
        self.assertEqual(self._gate(boom), 0)

    def test_malformed_proc_fails_open(self):
        self.assertEqual(self._gate(lambda: ("garbage", "garbage")), 0)

    def test_no_swap_box_judged_on_ram(self):
        mem = "MemTotal: 8000000 kB\nMemAvailable: 5000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
        self.assertEqual(self._gate(lambda: (mem, PSI_CLEAR)), 0)


class _FakeClock:
    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class TestSeats(unittest.TestCase):
    def test_only_system_orchestrators(self):
        seats = sr.system_orchestrators(INSTANCE, pwlookup=_pw)
        self.assertEqual([(s.persona, s.account, s.uid) for s in seats],
                         [("recovery", "claude-fallback", 1001), ("watchman", "codex-fallback", 1011)])

    def test_unknown_account_is_skipped_not_fatal(self):
        inst = {"roles": {"system-orchestrator-9": {"persona": "ghost", "account": "nobody-here"}}}
        def pw(name):
            raise KeyError(name)
        self.assertEqual(sr.system_orchestrators(inst, pwlookup=pw), [])


class TestRender(unittest.TestCase):
    def setUp(self):
        self.files = sr.render(sr.system_orchestrators(INSTANCE, pwlookup=_pw), root=Path("/"))
        self.by_path = {str(f.path): f for f in self.files}

    def test_paths(self):
        self.assertIn("/etc/systemd/system/user@1001.service.d/50-pantheon-sysorch-restart.conf", self.by_path)
        self.assertIn("/etc/systemd/system/user@1011.service.d/50-pantheon-sysorch-restart.conf", self.by_path)
        self.assertIn("/home/claude-fallback/.config/systemd/user/"
                      "pantheon-runtime-controller@recovery.service.d/50-pantheon-sysorch-backoff.conf", self.by_path)
        self.assertNotIn("user@1000", " ".join(self.by_path))  # orchestrator is NOT a system orch

    def test_root_dropin_contents(self):
        body = self.by_path["/etc/systemd/system/user@1001.service.d/50-pantheon-sysorch-restart.conf"].content
        for needle in ("Restart=on-failure", "RestartSteps=", "RestartMaxDelaySec=",
                       "ConditionPathExists=!/var/lib/pantheon/oom-hold/recovery",
                       "ExecStartPre=+", " gate", "TimeoutStartSec="):
            self.assertIn(needle, body)

    def test_controller_dropin_contents(self):
        body = self.by_path["/home/codex-fallback/.config/systemd/user/"
                            "pantheon-runtime-controller@watchman.service.d/50-pantheon-sysorch-backoff.conf"].content
        for needle in ("RestartSteps=", "RestartMaxDelaySec=", "StartLimitIntervalSec=0",
                       "ConditionPathExists=!/var/lib/pantheon/oom-hold/watchman", " gate"):
            self.assertIn(needle, body)

    def test_gate_timeout_fits_inside_start_timeout(self):
        for f in self.files:
            if f.path.suffix == ".conf":
                self.assertLess(sr.GATE_MAX_WAIT_S, sr.START_TIMEOUT_S, f.path)


class TestInstallRoundTrip(unittest.TestCase):
    def test_dry_run_writes_nothing_then_apply_check_uninstall(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seats = sr.system_orchestrators(INSTANCE, pwlookup=_pw)
            sr.install(seats, root=root, apply=False, chown=False, gate_src=Path(sr.__file__))
            self.assertEqual([p for p in root.rglob("*") if p.is_file()], [])
            self.assertEqual(sr.check(seats, root=root), 1)
            sr.install(seats, root=root, apply=True, chown=False, gate_src=Path(sr.__file__))
            self.assertEqual(sr.check(seats, root=root), 0)
            self.assertTrue((root / "var/lib/pantheon/oom-hold").is_dir())
            sr.uninstall(seats, root=root, apply=True)
            self.assertEqual(sr.check(seats, root=root), 1)
            left = [p for p in root.rglob("*.conf")]
            self.assertEqual(left, [])

    def test_check_detects_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seats = sr.system_orchestrators(INSTANCE, pwlookup=_pw)
            sr.install(seats, root=root, apply=True, chown=False, gate_src=Path(sr.__file__))
            p = root / "etc/systemd/system/user@1001.service.d/50-pantheon-sysorch-restart.conf"
            p.write_text(p.read_text().replace("Restart=on-failure", "Restart=no"))
            self.assertEqual(sr.check(seats, root=root), 1)


    def _installed(self, td):
        root = Path(td)
        seats = sr.system_orchestrators(INSTANCE, pwlookup=_pw)
        sr.install(seats, root=root, apply=True, chown=False, gate_src=Path(sr.__file__))
        return root, seats

    def _check_out(self, seats, root):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sr.check(seats, root=root)
        return rc, buf.getvalue()

    def test_check_reports_unreadable_not_missing_T5230(self):
        # T-5230: recovery (non-root) read watchman's drop-in in a 0750 home and was told
        # MISSING-OR-DRIFTED although it was installed and loaded.
        with tempfile.TemporaryDirectory() as td:
            root, seats = self._installed(td)
            target = sr.render(seats, root)[0].path
            real_read = Path.read_text
            def read_text(self, *a, **k):
                if self == target:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_read(self, *a, **k)
            with mock.patch.object(Path, "read_text", read_text):
                rc, out = self._check_out(seats, root)
        self.assertEqual(rc, sr.EXIT_UNREADABLE)
        self.assertIn(f"UNREADABLE {target}", out)
        self.assertNotIn("MISSING", out)
        self.assertIn("run as root", out)

    def test_check_real_problem_outranks_unreadable_T5230(self):
        with tempfile.TemporaryDirectory() as td:
            root, seats = self._installed(td)
            files = sr.render(seats, root)
            files[1].path.unlink()
            real_read = Path.read_text
            def read_text(self, *a, **k):
                if self == files[0].path:
                    raise PermissionError(13, "Permission denied", str(self))
                return real_read(self, *a, **k)
            with mock.patch.object(Path, "read_text", read_text):
                rc, out = self._check_out(seats, root)
        self.assertEqual(rc, 1)
        self.assertIn(f"MISSING {files[1].path}", out)
        self.assertIn(f"UNREADABLE {files[0].path}", out)

    def test_check_names_missing_and_drifted_separately_T5230(self):
        with tempfile.TemporaryDirectory() as td:
            root, seats = self._installed(td)
            files = sr.render(seats, root)
            files[0].path.write_text("drifted\n")
            files[1].path.unlink()
            rc, out = self._check_out(seats, root)
        self.assertEqual(rc, 1)
        self.assertIn(f"DRIFTED {files[0].path}", out)
        self.assertIn(f"MISSING {files[1].path}", out)


if __name__ == "__main__":
    unittest.main()
