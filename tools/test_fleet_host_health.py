"""Unit tests for tools/fleet_host_health.py (T-2454). Run: python3 -m unittest tools.test_fleet_host_health

pytest is not installed on the recovery seat — unittest only (reference_pytest_absent_use_unittest)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_host_health as m  # noqa: E402


class FakeOps:
    """Inject probe results and capture alerts, so no ssh/curl/ping runs in tests."""

    def __init__(self, reachable=None, alarms=None, now=1000):
        self._reachable = reachable or {}     # host name/ip -> True/False/None
        self._alarms = alarms or {}           # netdata base url -> list[str]/None
        self._now = now
        self.alerts = []                      # captured (msg, vocab, agent)

    def icmp_reachable(self, host):
        return self._reachable.get(host)

    def netdata_critical_alarms(self, url):
        return self._alarms.get(url)

    def alert(self, msg, vocab, agent="recovery"):
        self.alerts.append((msg, vocab, agent))

    def now(self):
        return self._now


BOXON = {"name": "boxon", "ping": "boxon", "netdata": "http://boxon:19999"}
PVE2 = {"name": "pve2", "ping": "pve2", "netdata": None}


class ClassifyHostTests(unittest.TestCase):
    def test_ok_reachable_only(self):
        d = m.classify_host(PVE2, reachable=True, critical_alarms=None)
        self.assertEqual(d["verdict"], m.OK)

    def test_ok_reachable_with_netdata_no_critical(self):
        d = m.classify_host(BOXON, reachable=True, critical_alarms=[])
        self.assertEqual(d["verdict"], m.OK)

    def test_unreachable_alerts(self):
        d = m.classify_host(PVE2, reachable=False, critical_alarms=None)
        self.assertEqual(d["verdict"], m.UNREACHABLE)
        self.assertIn(m.UNREACHABLE, m.ALERTING)

    def test_critical_lists_alarm_names(self):
        d = m.classify_host(BOXON, reachable=True, critical_alarms=["ram_usage", "disk_space"])
        self.assertEqual(d["verdict"], m.CRITICAL)
        self.assertIn("ram_usage", d["detail"])

    def test_unknown_when_probe_unrunnable(self):
        # reachable is None == "could not run ping", NOT "down" — must be UNKNOWN, not UNREACHABLE.
        d = m.classify_host(BOXON, reachable=None, critical_alarms=None)
        self.assertEqual(d["verdict"], m.UNKNOWN)

    def test_unknown_when_netdata_unreadable(self):
        # host pings but netdata unreadable (None, not []) -> UNKNOWN, the collector-is-down case.
        d = m.classify_host(BOXON, reachable=True, critical_alarms=None)
        self.assertEqual(d["verdict"], m.UNKNOWN)


class DisabledReasonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, obj):
        (self.state / name).write_text(json.dumps(obj))

    def test_absent_is_not_muted(self):
        self.assertIsNone(m.disabled_reason(self.state, 1000, m._DISABLE_NAME))

    def test_active_mute(self):
        self._write(m._DISABLE_NAME, {"reason": "pve2 maint", "until": "2999-01-01T00:00:00Z"})
        self.assertIsNotNone(m.disabled_reason(self.state, 1000, m._DISABLE_NAME))

    def test_expired_mute_does_not_apply(self):
        self._write(m._DISABLE_NAME, {"reason": "old", "until": "2000-01-01T00:00:00Z"})
        self.assertIsNone(m.disabled_reason(self.state, 4102444800, m._DISABLE_NAME))

    def test_malformed_until_fails_toward_alerting(self):
        self._write(m._DISABLE_NAME, {"reason": "typo", "until": "not-a-date"})
        self.assertIsNone(m.disabled_reason(self.state, 1000, m._DISABLE_NAME))


class FinishAndCooldownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, ops, cfg=PVE2, cooldown=100):
        return m.check_host_once(cfg, ops=ops, state_dir=self.state, now=ops.now(),
                                 cooldown_secs=cooldown, dry_run=False)

    def test_alerting_verdict_fires_alert(self):
        ops = FakeOps(reachable={"pve2": False})
        r = self._run(ops)
        self.assertEqual(r["verdict"], m.UNREACHABLE)
        self.assertTrue(r["alerted"])
        self.assertEqual(len(ops.alerts), 1)

    def test_ok_never_alerts(self):
        ops = FakeOps(reachable={"pve2": True})
        r = self._run(ops)
        self.assertEqual(r["verdict"], m.OK)
        self.assertFalse(r["alerted"])
        self.assertEqual(ops.alerts, [])

    def test_same_verdict_throttled_by_cooldown(self):
        ops = FakeOps(reachable={"pve2": False}, now=1000)
        self.assertTrue(self._run(ops)["alerted"])          # first fires
        ops._now = 1050                                     # < cooldown 100 later
        self.assertFalse(self._run(ops)["alerted"])         # throttled
        self.assertEqual(len(ops.alerts), 1)

    def test_same_verdict_speaks_after_cooldown(self):
        ops = FakeOps(reachable={"pve2": False}, now=1000)
        self._run(ops)
        ops._now = 1000 + 200                               # > cooldown 100
        self.assertTrue(self._run(ops)["alerted"])
        self.assertEqual(len(ops.alerts), 2)

    def test_changed_verdict_always_speaks(self):
        # UNREACHABLE then (host back, netdata critical) CRITICAL within cooldown -> must still fire.
        ops = FakeOps(reachable={"boxon": False}, now=1000)
        self.assertTrue(self._run(ops, cfg=BOXON)["alerted"])
        ops._reachable = {"boxon": True}
        ops._alarms = {"http://boxon:19999": ["ram_usage"]}
        ops._now = 1010                                     # well within cooldown 100
        r = self._run(ops, cfg=BOXON)
        self.assertEqual(r["verdict"], m.CRITICAL)
        self.assertTrue(r["alerted"])
        self.assertEqual(len(ops.alerts), 2)

    def test_dry_run_never_alerts(self):
        ops = FakeOps(reachable={"pve2": False})
        r = m.check_host_once(PVE2, ops=ops, state_dir=self.state, now=ops.now(),
                              cooldown_secs=100, dry_run=True)
        self.assertEqual(r["verdict"], m.UNREACHABLE)
        self.assertFalse(r["alerted"])
        self.assertEqual(ops.alerts, [])

    # --- T-2717: UNKNOWN streak gate (make netdata-unreadable pings less finnicky) ---
    def test_unknown_suppressed_until_streak_met(self):
        # A reachable host with an unreadable netdata reads UNKNOWN. It must PERSIST
        # UNKNOWN_ALERT_STREAK (3) consecutive checks before it pings — a transient
        # netdata blip (restart / brief load) stays quiet.
        ops = FakeOps(reachable={"boxon": True}, now=1000)  # netdata unreadable -> UNKNOWN
        r1 = self._run(ops, cfg=BOXON)
        self.assertEqual(r1["verdict"], m.UNKNOWN)
        self.assertFalse(r1["alerted"], "1st UNKNOWN (streak 1/3) must not alert")
        self.assertIn("streak 1/3", r1["detail"])
        ops._now = 1300
        self.assertFalse(self._run(ops, cfg=BOXON)["alerted"], "2nd (2/3) still quiet")
        ops._now = 1600
        r3 = self._run(ops, cfg=BOXON)
        self.assertTrue(r3["alerted"], "3rd consecutive UNKNOWN (3/3) alerts")
        self.assertEqual(len(ops.alerts), 1)

    def test_unreachable_is_not_streak_gated(self):
        # Only UNKNOWN is streak-gated; a host that is actually DOWN speaks on check 1.
        ops = FakeOps(reachable={"boxon": False}, now=1000)
        r = self._run(ops, cfg=BOXON)
        self.assertEqual(r["verdict"], m.UNREACHABLE)
        self.assertTrue(r["alerted"], "UNREACHABLE alerts immediately, no streak gate")

    def test_unknown_streak_resets_on_recovery(self):
        # UNKNOWN twice (2/3), then a readable netdata (OK) resets the streak, so a
        # later UNKNOWN starts counting from 1/3 again and stays quiet.
        ops = FakeOps(reachable={"boxon": True}, now=1000)      # UNKNOWN
        self._run(ops, cfg=BOXON)
        ops._now = 1300
        self._run(ops, cfg=BOXON)                              # streak 2/3
        ops._alarms = {"http://boxon:19999": []}               # netdata readable, no alarms -> OK
        ops._now = 1600
        self.assertEqual(self._run(ops, cfg=BOXON)["verdict"], m.OK)
        ops._alarms = {}                                       # unreadable again -> UNKNOWN
        ops._now = 1900
        r = self._run(ops, cfg=BOXON)
        self.assertEqual(r["verdict"], m.UNKNOWN)
        self.assertFalse(r["alerted"], "streak reset by OK → back to 1/3, quiet")
        self.assertEqual(ops.alerts, [])

    def test_per_host_mute_silences_only_that_host(self):
        (self.state / "fleet-host-health.pve2.disabled").write_text(
            json.dumps({"reason": "maint", "until": "2999-01-01T00:00:00Z"}))
        ops = FakeOps(reachable={"pve2": False, "boxon": False},
                      alarms={"http://boxon:19999": None})
        pve = self._run(ops, cfg=PVE2)
        box = self._run(ops, cfg=BOXON)
        self.assertEqual(pve["verdict"], m.DISABLED)
        self.assertFalse(pve["alerted"])
        self.assertEqual(box["verdict"], m.UNREACHABLE)     # other host still speaks
        self.assertTrue(box["alerted"])


class CheckAllTests(unittest.TestCase):
    def test_iterates_all_configured_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ops = FakeOps(reachable={"boxon": True, "pve2": True, "100.119.110.9": True},
                          alarms={"http://boxon:19999": [], "http://100.119.110.9:19999": []})
            results = m.check_all(ops=ops, state_dir=Path(tmp))
            self.assertEqual([r["name"] for r in results], [h["name"] for h in m.HOSTS])
            self.assertTrue(all(r["verdict"] == m.OK for r in results))


if __name__ == "__main__":
    unittest.main()
