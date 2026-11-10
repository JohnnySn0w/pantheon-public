"""Tests for tools/pve2_heat_monitor.py (T-5309). Run: python3 -m unittest tools.test_pve2_heat_monitor

The facts dicts mirror real netdata parent replies captured 2026-10-20 (pve2 package
10-min average 74.56C, max 87C; gpu-ml A2000 71.4C).
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import pve2_heat_monitor as hm

NOW = 1_800_000_000.0


def facts(cpu=74.56, gpu=71.4):
    return {"now": NOW, "readings": {"cpu_package": cpu, "gpu": gpu}}



_TAILNET_PATCHES = []


def setUpModule():
    # T-5385: pin "this host is on the tailnet" so these tests never read the real box.
    for name, kw in (("off_tailnet", {"return_value": None}), ("note_reachable", {}),
                     ("report_off_tailnet", {})):
        p = mock.patch.object(hm.tailnet_self, name, **kw)
        p.start()
        _TAILNET_PATCHES.append(p)


def tearDownModule():
    for p in _TAILNET_PATCHES:
        p.stop()

class EvaluateTests(unittest.TestCase):
    def test_normal_temps_are_ok(self):
        self.assertEqual(hm.evaluate(facts()), [])

    def test_sustained_hot_cpu_and_gpu(self):
        self.assertEqual(hm.evaluate(facts(cpu=91.2, gpu=86.0)), [
            "cpu_package: 10-min average 91.2C (limit 90C)",
            "gpu: 10-min average 86.0C (limit 85C)"])

    def test_missing_reading_fails_closed(self):
        self.assertEqual(hm.evaluate(facts(gpu=None)), ["gpu: no temperature data from netdata"])


class AlertStateTests(unittest.TestCase):
    def test_alerts_on_transition_then_holds_quiet_then_realerts_after_6h(self):
        st = {}
        self.assertTrue(hm.should_alert(["x"], st, NOW))
        self.assertFalse(hm.should_alert(["x"], st, NOW + 600))
        self.assertTrue(hm.should_alert(["x"], st, NOW + 6 * 3600 + 1))

    def test_recovery_clears_state_so_next_problem_alerts(self):
        st = {}
        hm.should_alert(["x"], st, NOW)
        self.assertFalse(hm.should_alert([], st, NOW + 600))
        self.assertTrue(hm.should_alert(["x"], st, NOW + 1200))


class MainTests(unittest.TestCase):
    def _main(self, run, state_text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        sp = d / "state.json"
        if state_text is not None:
            sp.write_text(state_text)
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(hm.subprocess, "run", run), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = hm.main(["--state", str(sp)])
        return rc, out.getvalue(), err.getvalue(), sp

    def test_ok(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(facts()), ""))
        rc, out, _, _ = self._main(run)
        self.assertEqual(rc, 0)
        self.assertIn("OK cpu_package 74.6C, gpu 71.4C", out)
        self.assertIn("pct exec 201", run.call_args.args[0][-1])
        self.assertIn("_temperature", run.call_args.kwargs["input"])

    def test_hot_alerts_once_then_quiet_while_still_hot(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(facts(gpu=88)), ""))
        rc1, _, err1, sp = self._main(run)
        self.assertEqual(rc1, 1)
        self.assertIn("FAIL gpu: 10-min average 88.0C", err1)
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(hm.subprocess, "run", run), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc2 = hm.main(["--state", str(sp)])
        self.assertEqual(rc2, 0)
        self.assertIn("still hot (alerted", out.getvalue())

    def test_unreachable_fails_closed(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "no route"))
        rc, _, err, _ = self._main(run)
        self.assertEqual(rc, 1)
        self.assertIn("could not read temperatures", err)

    def test_unreachable_while_alerted_says_unreadable_not_hot(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "no route"))
        _, _, _, sp = self._main(run)
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(hm.subprocess, "run", run), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc2 = hm.main(["--state", str(sp)])
        self.assertEqual(rc2, 0)
        self.assertIn("still unreadable (alerted", out.getvalue())
        self.assertNotIn("still hot", out.getvalue())

    def test_unreachable_while_this_host_is_off_the_tailnet_is_not_a_heat_alert(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "timed out"))
        with mock.patch.object(hm.tailnet_self, "off_tailnet", return_value="NeedsLogin"), \
                mock.patch.object(hm.tailnet_self, "report_off_tailnet") as report:
            rc, out, err, sp = self._main(run)
        self.assertEqual(rc, 0)
        self.assertNotIn("FAIL", err)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[:2], ("pve2-heat", "NeedsLogin"))

    def test_enospc_preserves_alert_state_and_recovery_stays_suppressed(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(facts(gpu=88)), ""))
        with tempfile.TemporaryDirectory() as tmp:
            sp = Path(tmp) / "state.json"
            original = json.dumps({"alerted_at": NOW - 60})
            sp.write_text(original)
            with mock.patch.object(hm.time, "time", return_value=NOW), \
                    mock.patch.object(hm.subprocess, "run", run), \
                    mock.patch.object(hm.json, "dump",
                                      side_effect=OSError(errno.ENOSPC, "No space left on device")):
                with self.assertRaises(OSError) as raised:
                    hm.main(["--state", str(sp)])
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(sp.read_text(), original)
            self.assertEqual(list(sp.parent.glob(f".{sp.name}.*")), [])

            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(hm.time, "time", return_value=NOW), \
                    mock.patch.object(hm.subprocess, "run", run), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = hm.main(["--state", str(sp)])
            self.assertEqual(rc, 0)
            self.assertIn("still hot (alerted", out.getvalue())
            self.assertEqual(json.loads(sp.read_text()), {"alerted_at": NOW - 60})



if __name__ == "__main__":
    unittest.main()
