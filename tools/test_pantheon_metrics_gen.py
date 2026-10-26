#!/usr/bin/env python3
"""Tests for tools/pantheon_metrics_gen.py.

Focus: the own-account-direct seat-argv fix (T-4009) — a per-seat cross-account
tool must NOT `sudo -u <acct>` its OWN row, because sudo-to-self is unconfigured
and false-nulls (see the bug catalog entry referenced in _seat_argv). Regression
guard for the 4th instance of that class.
"""
import getpass
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.pantheon_metrics_gen as pmg
import tools.seat_exec as se  # getuser now lives here (T-4066 retrofit)


class SeatArgvOwnAccountDirect(unittest.TestCase):
    def test_own_account_no_sudo(self) -> None:
        me = getpass.getuser()
        self.assertEqual(pmg._seat_argv(me, "echo hi"), ["bash", "-c", "echo hi"])

    def test_other_account_uses_sudo(self) -> None:
        other = "definitely-not-me-account"
        self.assertEqual(
            pmg._seat_argv(other, "echo hi"),
            ["sudo", "-u", other, "bash", "-c", "echo hi"],
        )

    def test_own_account_never_prefixes_sudo(self) -> None:
        # The whole point: no 'sudo' token anywhere for our own seat.
        self.assertNotIn("sudo", pmg._seat_argv(getpass.getuser(), "true"))


class JsonlFreshRoutesThroughSeatArgv(unittest.TestCase):
    def _fake_run(self, stdout: str, rc: int = 0) -> MagicMock:
        r = MagicMock()
        r.stdout = stdout
        r.returncode = rc
        return r

    def test_own_seat_runs_directly(self) -> None:
        me = getpass.getuser()
        captured = {}

        def fake_run(argv):
            captured["argv"] = argv
            return self._fake_run("0\n")  # ancient mtime -> not fresh, but argv is the point

        with patch.object(pmg, "_run", side_effect=fake_run):
            pmg._jsonl_fresh(me)
        self.assertEqual(captured["argv"][:1], ["bash"])  # NO sudo for own seat
        self.assertNotIn("sudo", captured["argv"])

    def test_peer_seat_goes_through_sudo(self) -> None:
        captured = {}

        def fake_run(argv):
            captured["argv"] = argv
            return self._fake_run("0\n")

        with patch.object(pmg, "_run", side_effect=fake_run):
            pmg._jsonl_fresh("some-peer-acct")
        self.assertEqual(captured["argv"][:3], ["sudo", "-u", "some-peer-acct"])

    def test_own_seat_fresh_mtime_reads_active(self) -> None:
        # With the fix, the own seat gets a REAL recent mtime -> fresh=True.
        # (Pre-fix, self-sudo emptied stdout -> float('') -> False -> always idle.)
        me = getpass.getuser()
        import time as _t
        recent = str(_t.time())
        with patch.object(pmg, "_run", return_value=self._fake_run(recent + "\n")):
            self.assertTrue(pmg._jsonl_fresh(me))


class SvcShowRoutesThroughSeatArgv(unittest.TestCase):
    def test_peer_service_uses_sudo(self) -> None:
        captured = {}

        def fake_run(argv):
            captured["argv"] = argv
            r = MagicMock()
            r.stdout = "active\n"
            r.returncode = 0
            return r

        # Pin the running user so "claude" is a peer on every seat, including
        # the claude seat itself (where it would otherwise be the own account).
        with patch.object(pmg, "_run", side_effect=fake_run), \
                patch.object(se.getpass, "getuser", return_value="some-other-seat"):
            pmg._svc_show("claude", "echo.service", "ActiveState")
        self.assertEqual(captured["argv"][:3], ["sudo", "-u", "claude"])



class ProbeFailureIsUnknownNotNegative(unittest.TestCase):
    """T-4070: a FAILED cross-account probe (sudo/bus error, rc != 0) must not render
    as a real negative ('down'/'idle')."""

    def _rc(self, stdout: str, rc: int) -> MagicMock:
        return MagicMock(returncode=rc, stdout=stdout, stderr="" if rc == 0 else "sudo: a password is required")

    def test_svc_show_failed_probe_is_none_real_inactive_is_value(self) -> None:
        with patch.object(pmg, "_run", return_value=self._rc("", 1)):
            self.assertIsNone(pmg._svc_show("peer", "echo.service", "ActiveState"))
        with patch.object(pmg, "_run", return_value=self._rc("inactive\n", 0)):
            self.assertEqual(pmg._svc_show("peer", "echo.service", "ActiveState"), "inactive")

    def test_jsonl_fresh_failed_probe_is_none(self) -> None:
        with patch.object(pmg, "_run", return_value=self._rc("", 1)):
            self.assertIsNone(pmg._jsonl_fresh("peer"))
        with patch.object(pmg, "_run", return_value=self._rc("", 0)):  # no transcript at all
            self.assertFalse(pmg._jsonl_fresh("peer"))

    def _collect(self, emit: bool, svc_active, jsonl_fresh, uid: int = 1234):
        roster = [{"label": "orchestrator", "acct": "claude", "kind": "agent"},
                  {"label": "echo", "acct": "claude", "kind": "service", "service": "echo.service"}]

        def svc(acct, service, prop):
            return svc_active if prop == "ActiveState" else "0"
        with patch.object(pmg, "EMIT_UNKNOWN", emit), \
             patch.object(pmg, "_svc_show", side_effect=svc), \
             patch.object(pmg, "_jsonl_fresh", return_value=jsonl_fresh), \
             patch.object(pmg, "_uid", return_value=uid), \
             patch.object(pmg, "_account_pids", return_value=[]), \
             patch.object(pmg, "_has_agent_proc", return_value=True), \
             patch.object(pmg, "_mem_pct_account", return_value=0.0), \
             patch.object(pmg, "_host_up", return_value=True), \
             patch.object(pmg.time, "sleep"):
            doc = pmg.collect(roster, sample_sec=0.01)
        return {a["label"]: a["state"] for a in doc["agents"]}, doc

    def test_failed_probes_emit_unknown_when_enabled(self) -> None:
        states, doc = self._collect(emit=True, svc_active=None, jsonl_fresh=None)
        self.assertEqual(states, {"orchestrator": "unknown", "echo": "unknown"})
        self.assertEqual(doc["cluster"]["agents_up"], 0)  # unknown is never counted as up
        pmg.leak_scan(doc)  # 'unknown' is allowlisted

    def test_failed_uid_lookup_is_unknown(self) -> None:
        states, _ = self._collect(emit=True, svc_active="active", jsonl_fresh=True, uid=-1)
        self.assertEqual(states["orchestrator"], "unknown")

    def test_gate_off_renders_legacy_down_and_says_why(self) -> None:
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            states, _ = self._collect(emit=False, svc_active=None, jsonl_fresh=None)
        self.assertEqual(states, {"orchestrator": "down", "echo": "down"})
        self.assertIn("probe FAILED", err.getvalue())

    def test_agent_cpu_is_clamped_so_leak_scan_never_refuses_the_write(self) -> None:
        with patch.object(pmg, "_pids_jiffies", side_effect=[0, 10_000]), \
             patch.object(pmg, "_total_cpu_jiffies", side_effect=[0, 5]):
            roster = [{"label": "orchestrator", "acct": "claude", "kind": "agent"}]
            with patch.object(pmg, "_uid", return_value=1), patch.object(pmg, "_account_pids", return_value=[1]), \
                 patch.object(pmg, "_has_agent_proc", return_value=True), patch.object(pmg, "_jsonl_fresh", return_value=False), \
                 patch.object(pmg, "_mem_pct_account", return_value=0.0), patch.object(pmg, "_host_up", return_value=True), \
                 patch.object(pmg.time, "sleep"):
                doc = pmg.collect(roster, sample_sec=0.01)
        self.assertEqual(doc["agents"][0]["cpu_pct"], 100.0)
        pmg.leak_scan(doc)

    def test_real_answers_unchanged(self) -> None:
        states, doc = self._collect(emit=True, svc_active="inactive", jsonl_fresh=False)
        self.assertEqual(states, {"orchestrator": "idle", "echo": "down"})
        self.assertEqual(doc["cluster"]["agents_up"], 1)
        states, _ = self._collect(emit=True, svc_active="active", jsonl_fresh=True)
        self.assertEqual(states, {"orchestrator": "active", "echo": "idle"})

if __name__ == "__main__":
    unittest.main()
