"""Tests for tools/health_watcher_sudo_probe.py (T-5415 item b).

subprocess is ALWAYS mocked: these tests never run sudo, never touch sudoers.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from tools import health_watcher as hw
from tools import health_watcher_sudo_probe as probe

PERSONAS = [("alpha", "acct-a", None), ("beta", "acct-b", None), ("gamma", "acct-c", None)]


def _cp(rc, out="", err=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr=err)


class _Fake:
    """Stands in for subprocess.run; records every argv."""

    def __init__(self, by_acct):
        self.by_acct, self.calls = by_acct, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        acct = argv[argv.index("-u") + 1]
        r = self.by_acct[acct]
        if isinstance(r, BaseException):
            raise r
        return r


class ProbeTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(probe.pwd, "getpwnam", side_effect=lambda a: mock.Mock(pw_uid=2000 + ord(a[-1])))
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(probe, "_self_account", return_value="recovery-x")
        s.start()
        self.addCleanup(s.stop)

    def _run(self, by_acct, personas=PERSONAS):
        fake = _Fake(by_acct)
        with mock.patch.object(probe.subprocess, "run", fake):
            res = probe.probe_all(personas)
        return res, fake

    def test_all_ok(self):
        res, _ = self._run({a: _cp(0, "/usr/bin/env") for a in ("acct-a", "acct-b", "acct-c")})
        self.assertEqual([r.status for r in res], ["OK"] * 3)

    def test_missing_and_unknown_classified(self):
        res, _ = self._run({
            "acct-a": _cp(1, "", "Sorry, user recovery may not run sudo"),
            "acct-b": _cp(1, "", "sudo: a password is required"),
            "acct-c": subprocess.TimeoutExpired("sudo", 10),
        })
        self.assertEqual([r.status for r in res], ["MISSING", "MISSING", "UNKNOWN"])

    def test_unknown_user_and_oserror_are_unknown(self):
        res, _ = self._run({"acct-a": _cp(1, "", "sudo: unknown user acct-a"),
                            "acct-b": FileNotFoundError("sudo"),
                            "acct-c": _cp(127, "", "")})
        self.assertEqual([r.status for r in res], ["UNKNOWN"] * 3)

    def test_argv_is_non_mutating_list_form_and_never_without_n(self):
        _, fake = self._run({a: _cp(0) for a in ("acct-a", "acct-b", "acct-c")})
        self.assertEqual(len(fake.calls), 3)
        for argv, kw in fake.calls:
            self.assertEqual(argv[:3], ["sudo", "-n", "-l"])
            self.assertIsInstance(argv, list)
            self.assertNotIn("shell", kw)
            self.assertEqual(kw["stdin"], subprocess.DEVNULL)
            self.assertIn("timeout", kw)
            self.assertNotIn("-S", argv)
            self.assertNotIn("-i", argv)
            self.assertNotIn("-s", argv)
            self.assertNotIn("-v", argv)
            self.assertNotIn("-k", argv)

    def test_checked_command_is_the_watchers_real_tap_command(self):
        _, fake = self._run({a: _cp(0) for a in ("acct-a", "acct-b", "acct-c")})
        for argv, _kw in fake.calls:
            self.assertEqual(argv[3:3 + 2], ["-u", argv[4]])
            self.assertEqual(argv[5], "env")
            self.assertIn(hw._TAP_SRC, argv)
            i = argv.index("python3")
            self.assertEqual(argv[i:i + 3], ["python3", "-I", "-c"])

    def test_self_account_needs_no_sudo(self):
        with mock.patch.object(probe, "_self_account", return_value="acct-a"):
            res, fake = self._run({a: _cp(0) for a in ("acct-b", "acct-c")})
        self.assertEqual(res[0].status, "OK")
        self.assertEqual(len(fake.calls), 2)

    def test_exit_code_nonzero_on_missing_or_unknown(self):
        with mock.patch.object(probe, "fleet_personas", return_value=PERSONAS), \
             mock.patch.object(probe.subprocess, "run", _Fake({a: _cp(0) for a in ("acct-a", "acct-b", "acct-c")})), \
             mock.patch("builtins.print"):
            self.assertEqual(probe.main([]), 0)
        bad = {"acct-a": _cp(0), "acct-b": _cp(1, "", "may not run"), "acct-c": _cp(0)}
        with mock.patch.object(probe, "fleet_personas", return_value=PERSONAS), \
             mock.patch.object(probe.subprocess, "run", _Fake(bad)), mock.patch("builtins.print"):
            self.assertEqual(probe.main([]), 1)

    def test_empty_fleet_is_nonzero(self):
        with mock.patch.object(probe, "fleet_personas", return_value=[]), mock.patch("builtins.print"):
            self.assertEqual(probe.main([]), 1)

    def test_report_names_each_account_and_status_without_tap_source(self):
        res, _ = self._run({"acct-a": _cp(0), "acct-b": _cp(1, "", "may not run"),
                            "acct-c": subprocess.TimeoutExpired("sudo", 1)})
        lines = [probe.format_result(r) for r in res]
        self.assertTrue(lines[0].startswith("OK") and "acct-a" in lines[0])
        self.assertTrue(lines[1].startswith("MISSING") and "acct-b" in lines[1])
        self.assertTrue(lines[2].startswith("UNKNOWN") and "acct-c" in lines[2])
        self.assertTrue(all("import dbus" not in l for l in lines))

    def test_import_runs_nothing(self):
        # Module import must not have invoked subprocess (the suite imported it at top).
        with mock.patch.object(probe.subprocess, "run") as r:
            import importlib
            importlib.reload(probe)
            r.assert_not_called()


if __name__ == "__main__":
    unittest.main()
