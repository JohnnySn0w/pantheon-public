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


class ExpectedDownExcludedFromTotal(unittest.TestCase):
    """T-5655: an ORG-106 expected-down (deep-sleep, asleep) seat that the probe confirms
    down is omitted from agents_total; everything else counts exactly as before."""

    NOW = "2026-11-04T12:00:00Z"

    T0 = 1_800_000_000  # fixed "now" for the staleness maths

    def _write(self, d, persona="orchestrator", **doc):
        import json
        (Path(d) / f"{persona}.json").write_text(json.dumps(doc))

    def _ed(self, doc_or_none, now_offset=0, persona="orchestrator", raw=None):
        """_expected_down against a temp health dir holding at most one record."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            if raw is not None:
                (Path(d) / f"{persona}.json").write_text(raw)
            elif doc_or_none is not None:
                self._write(d, persona, **doc_or_none)
            return pmg._expected_down(persona, health_dir=d, now=self.T0 + now_offset)


    # --- the pure count ---
    def test_asleep_down_seat_leaves_total_and_up_unchanged(self) -> None:
        entries = [("active", False)] * 6 + [("down", True)]
        self.assertEqual(pmg.count_cluster(entries), (6, 6))

    def test_non_sleep_down_seat_still_lowers_up(self) -> None:
        entries = [("active", False)] * 6 + [("down", False)]
        self.assertEqual(pmg.count_cluster(entries), (6, 7))

    def test_mixed_only_the_asleep_seat_is_excluded(self) -> None:
        entries = [("active", False)] * 5 + [("down", True), ("down", False)]
        self.assertEqual(pmg.count_cluster(entries), (5, 6))

    def test_live_expected_down_seat_is_counted_up(self) -> None:
        self.assertEqual(pmg.count_cluster([("idle", True), ("active", False)]), (2, 2))

    def test_unknown_stays_in_total_even_if_asleep(self) -> None:
        self.assertEqual(pmg.count_cluster([("unknown", True), ("active", False)]), (1, 2))

    def test_invariants_and_edges(self) -> None:
        self.assertEqual(pmg.count_cluster([("down", True)] * 3), (0, 0))  # all asleep
        self.assertEqual(pmg.count_cluster([]), (0, 0))
        up, total = pmg.count_cluster([("down", True), ("down", False), ("idle", True), ("unknown", False)])
        self.assertTrue(0 <= up <= total)

    # --- the read: health_export record, fresh-only, fail-open ---
    def test_fresh_expected_down_true(self) -> None:
        self.assertTrue(self._ed({"exported_at": self.T0 - 30, "expected_down": True}))

    def test_fresh_expected_down_false(self) -> None:
        self.assertFalse(self._ed({"exported_at": self.T0 - 30, "expected_down": False}))

    def test_staleness_boundary(self) -> None:
        self.assertEqual(pmg.HEALTH_RECORD_MAX_AGE_SEC, 180)
        rec = {"exported_at": self.T0 - 180, "expected_down": True}
        self.assertTrue(self._ed(rec))                       # exactly 180s old: still usable
        self.assertFalse(self._ed(rec, now_offset=1))        # 181s old: stale, ignored

    def test_future_dated_record_is_ignored(self) -> None:
        self.assertFalse(self._ed({"exported_at": self.T0 + 60, "expected_down": True}))

    def test_missing_file_fails_open(self) -> None:
        self.assertFalse(self._ed(None))
        self.assertFalse(pmg._expected_down("orchestrator", health_dir="/nonexistent-dir-xyz", now=self.T0))
        with patch.object(pmg, "HEALTH_DIR", None):
            self.assertFalse(pmg._expected_down("orchestrator", now=self.T0))

    def test_malformed_json_fails_open(self) -> None:
        self.assertFalse(self._ed(None, raw="{not json"))
        self.assertFalse(self._ed(None, raw="[]"))
        self.assertFalse(self._ed(None, raw=""))

    def test_non_bool_expected_down_fails_open(self) -> None:
        for bad in ("true", 1, "yes", None, [True]):
            self.assertFalse(self._ed({"exported_at": self.T0, "expected_down": bad}), bad)

    def test_missing_or_bad_exported_at_fails_open(self) -> None:
        self.assertFalse(self._ed({"expected_down": True}))
        self.assertFalse(self._ed({"exported_at": "2026", "expected_down": True}))
        self.assertFalse(self._ed({"exported_at": True, "expected_down": True}))
        self.assertFalse(self._ed({"exported_at": None, "expected_down": True}))

    def test_dir_comes_from_health_export_constant(self) -> None:
        import tools.health_export as he
        self.assertEqual(pmg.HEALTH_DIR, he.DEFAULT_OUT_DIR)

    def test_gen_has_no_sudo_read_path_for_asleep(self) -> None:
        import inspect
        self.assertNotIn("sudo", inspect.getsource(pmg._expected_down))


    # --- collect() end to end ---
    def _collect(self, states, asleep_labels, probe_unknown=False):
        roster = [{"label": l, "acct": "a-" + l, "kind": "agent"} for l in states]
        roster.append({"label": "echo", "acct": "claude", "kind": "service", "service": "echo.service"})
        asked = []

        def exp(lbl, runner=None):
            asked.append(lbl)
            return lbl in asleep_labels
        uids = {"a-" + l: i + 100 for i, l in enumerate(states)}
        by_uid = {v: k[2:] for k, v in uids.items()}
        with patch.object(pmg, "_uid", side_effect=lambda acct: uids[acct]), \
             patch.object(pmg, "_account_pids", return_value=[]), \
             patch.object(pmg, "_has_agent_proc", side_effect=lambda uid: states[by_uid[uid]] != "down"), \
             patch.object(pmg, "_jsonl_fresh", return_value=None if probe_unknown else True), \
             patch.object(pmg, "_svc_show", side_effect=lambda a, s, p: "active" if p == "ActiveState" else "0"), \
             patch.object(pmg, "_mem_pct_account", return_value=0.0), \
             patch.object(pmg, "_host_up", return_value=True), \
             patch.object(pmg, "_expected_down", side_effect=exp), \
             patch.object(pmg.time, "sleep"):
            doc = pmg.collect(roster, sample_sec=0.01)
        return doc, asked

    def test_collect_asleep_seat_excluded_state_stays_down_and_leak_scan_ok(self) -> None:
        doc, asked = self._collect({"orchestrator": "up", "specialist": "up", "archivist": "down"}, {"archivist"})
        self.assertEqual(doc["cluster"]["agents_total"], 3)   # orchestrator, specialist, echo
        self.assertEqual(doc["cluster"]["agents_up"], 3)
        self.assertEqual({a["label"]: a["state"] for a in doc["agents"]}["archivist"], "down")
        self.assertEqual(asked, ["archivist"])  # only the confirmed-down seat is queried
        self.assertTrue(pmg.leak_scan(doc))

    def test_collect_plain_down_seat_still_lowers_up(self) -> None:
        doc, _ = self._collect({"orchestrator": "up", "archivist": "down"}, set())
        self.assertEqual((doc["cluster"]["agents_up"], doc["cluster"]["agents_total"]), (2, 3))
        self.assertTrue(pmg.leak_scan(doc))

    def test_collect_failed_probe_never_excluded_as_asleep(self) -> None:
        # EMIT_UNKNOWN off: unknown is remapped to legacy "down" -- it must STILL count in total.
        with patch.object(pmg, "EMIT_UNKNOWN", False):
            doc, asked = self._collect({"orchestrator": "up"}, {"orchestrator"}, probe_unknown=True)
        self.assertEqual(doc["cluster"]["agents_total"], 2)
        self.assertEqual(asked, [])
        self.assertTrue(pmg.leak_scan(doc))

    def test_leak_scan_still_rejects_up_over_total(self) -> None:
        doc, _ = self._collect({"orchestrator": "up"}, set())
        doc["cluster"]["agents_up"] = doc["cluster"]["agents_total"] + 1
        with self.assertRaises(AssertionError):
            pmg.leak_scan(doc)

    def test_echo_service_never_asked(self) -> None:
        _, asked = self._collect({"orchestrator": "up"}, set())
        self.assertNotIn("echo", asked)


if __name__ == "__main__":
    unittest.main()
