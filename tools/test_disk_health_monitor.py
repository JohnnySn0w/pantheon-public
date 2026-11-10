"""Tests for tools/disk_health_monitor.py (T-4997). Run: python3 -m unittest tools.test_disk_health_monitor

Fixtures are real `smartctl -j -a` captures from pve2 on 2026-10-20: sda (MX500 SSD,
1 UDMA CRC from a cable), sdb (the failing ST1000DM003: 24 realloc, 1520 pending),
sdc (ST3000DM008 archive, clean), sdd (USB stick, no SMART through its bridge).
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import disk_health_monitor as dhm

FIX = Path(__file__).resolve().parent / "fixtures" / "disk_health"
NOW = 1_800_000_000.0
DAY = 86400.0


def smart(name):
    return json.loads((FIX / f"pve2-{name}.json").read_text())


def facts(*, kernel=None, tran_usb=("sdd",)):
    disks = []
    for n in ("sda", "sdb", "sdc", "sdd"):
        disks.append({"name": n, "tran": "usb" if n in tran_usb else "sata", "smart": smart(n)})
    return {"host": "pve2", "now": NOW, "disks": disks, "kernel_errors": kernel or []}


SDB_ACK = {"W1D200FL": {"attrs": {"5": 24, "197": 1520, "198": 1520}, "reason": "failing, pull pending"}}
SDA_ACK = {"2350E88927AF": {"attrs": {"199": 1}, "reason": "cable CRC"}}


class SummariseTests(unittest.TestCase):
    def test_reads_defects_wear_and_written_from_real_captures(self):
        s = {d["serial"]: d for d in dhm.summarise(facts())}
        sda, sdb, sdc = s["2350E88927AF"], s["W1D200FL"], s["Z505HAED"]
        self.assertEqual(sdb["defects"], {"5": 24, "187": 0, "197": 1520, "198": 1520, "199": 0})
        self.assertEqual(sda["defects"]["199"], 1)
        self.assertEqual(sda["life_used_pct"], 3)          # attr 202 normalized 97
        self.assertEqual(sdc["life_used_pct"], 87)         # attr 193 normalized 13
        lbas = next(a["raw"]["value"] for a in smart("sda")["ata_smart_attributes"]["table"] if a["id"] == 246)
        self.assertEqual(sda["written_bytes"], lbas * 512)
        self.assertEqual(sda["tbw_used_pct"], round(100 * lbas * 512 / 360e12, 1))
        self.assertEqual(sdc["kind"], "hdd")
        self.assertNotIn("60E1DB06", s)                    # usb stick skipped


class EvaluateTests(unittest.TestCase):
    def test_failing_sdb_fires_without_ack_and_ignores_passed_verdict(self):
        probs = dhm.evaluate(facts(), ack=SDA_ACK, history=[])
        self.assertTrue(any("W1D200FL" in p and "197 Current_Pending=1520" in p for p in probs), probs)
        self.assertTrue(any("W1D200FL" in p and "5 Reallocated=24" in p for p in probs), probs)
        self.assertFalse(any("2350E88927AF" in p for p in probs), probs)

    def test_acked_counts_are_quiet_but_any_rise_fires(self):
        ack = {**SDA_ACK, **SDB_ACK}
        self.assertEqual(dhm.evaluate(facts(), ack=ack, history=[]), [])
        f = facts()
        f["disks"][1]["smart"]["ata_smart_attributes"]["table"] = [
            dict(a, raw={**a["raw"], "value": 1530}) if a["id"] == 197 else a
            for a in f["disks"][1]["smart"]["ata_smart_attributes"]["table"]]
        probs = dhm.evaluate(f, ack=ack, history=[])
        self.assertEqual(len(probs), 1)
        self.assertIn("197 Current_Pending=1530 (acked 1520)", probs[0])

    def test_kernel_link_errors_fire(self):
        probs = dhm.evaluate(facts(kernel=["ata2: hard resetting link"]), ack={**SDA_ACK, **SDB_ACK}, history=[])
        self.assertEqual(probs, ["pve2: 1 kernel ATA error line(s) in the last 26h, e.g. ata2: hard resetting link"])

    def test_unreadable_smart_on_a_sata_disk_fails_closed(self):
        f = facts(tran_usb=())
        probs = dhm.evaluate(f, ack={**SDA_ACK, **SDB_ACK}, history=[])
        self.assertEqual(len(probs), 1)
        self.assertIn("sdd", probs[0])
        self.assertIn("could not read SMART", probs[0])

    def test_life_used_threshold(self):
        f = facts()
        tbl = f["disks"][2]["smart"]["ata_smart_attributes"]["table"]
        f["disks"][2]["smart"]["ata_smart_attributes"]["table"] = [
            dict(a, value=8) if a["id"] == 193 else a for a in tbl]
        probs = dhm.evaluate(f, ack={**SDA_ACK, **SDB_ACK}, history=[])
        self.assertEqual(len(probs), 1)
        self.assertIn("Z505HAED", probs[0])
        self.assertIn("92% of rated life used", probs[0])

    def test_daily_write_budget_uses_history_delta(self):
        ack = {**SDA_ACK, **SDB_ACK}
        cur = {d["serial"]: d for d in dhm.summarise(facts())}
        sda_now = cur["2350E88927AF"]["written_bytes"]
        prev = {"now": NOW - DAY, "host": "pve2",
                "disks": {"2350E88927AF": {"written_bytes": sda_now - 50e9}}}
        self.assertEqual(dhm.evaluate(facts(), ack=ack, history=[prev]), [])
        prev["disks"]["2350E88927AF"]["written_bytes"] = sda_now - 400e9
        probs = dhm.evaluate(facts(), ack=ack, history=[prev])
        self.assertEqual(len(probs), 1)
        self.assertIn("wrote 400 GB/day", probs[0])
        self.assertIn("budget 197 GB/day", probs[0])

    def _sda_hist(self, week_gb_per_day=None, day_gb=None):
        cur = {d["serial"]: d for d in dhm.summarise(facts())}
        now_b = cur["2350E88927AF"]["written_bytes"]
        hist = []
        if week_gb_per_day is not None:
            hist.append({"now": NOW - 7 * DAY, "host": "pve2",
                         "disks": {"2350E88927AF": {"written_bytes": now_b - 7 * week_gb_per_day * 1e9}}})
        if day_gb is not None:
            hist.append({"now": NOW - DAY, "host": "pve2",
                         "disks": {"2350E88927AF": {"written_bytes": now_b - day_gb * 1e9}}})
        return hist

    def test_ssd_noisy_day_under_weekly_budget_is_quiet(self):
        """T-5919: 2026-11-02 paged on one 212 GB day while the week averaged ~147 GB/day."""
        hist = self._sda_hist(week_gb_per_day=147, day_gb=212)
        self.assertEqual(dhm.evaluate(facts(), ack={**SDA_ACK, **SDB_ACK}, history=hist), [])

    def test_ssd_sustained_weekly_overage_fires(self):
        hist = self._sda_hist(week_gb_per_day=250, day_gb=250)
        probs = dhm.evaluate(facts(), ack={**SDA_ACK, **SDB_ACK}, history=hist)
        self.assertEqual(len(probs), 1)
        self.assertIn("2350E88927AF", probs[0])
        self.assertIn("wrote 250 GB/day", probs[0])

    def test_ssd_runaway_day_fires_even_when_week_is_under_budget(self):
        hist = self._sda_hist(week_gb_per_day=150, day_gb=700)
        probs = dhm.evaluate(facts(), ack={**SDA_ACK, **SDB_ACK}, history=hist)
        self.assertEqual(len(probs), 1)
        self.assertIn("wrote 700 GB in a day", probs[0])

    def test_write_budget_ignores_samples_younger_than_20h(self):
        cur = {d["serial"]: d for d in dhm.summarise(facts())}
        prev = {"now": NOW - 3600, "host": "pve2",
                "disks": {"2350E88927AF": {"written_bytes": cur["2350E88927AF"]["written_bytes"] - 400e9}}}
        self.assertEqual(dhm.evaluate(facts(), ack={**SDA_ACK, **SDB_ACK}, history=[prev]), [])

    def test_hdd_workload_budget_uses_weekly_window_not_backup_burst(self):
        """A weekly full backup is annual workload, not a one-day sustained rate."""
        ack = {**SDA_ACK, **SDB_ACK}
        cur = {d["serial"]: d for d in dhm.summarise(facts())}
        hdd_now = cur["Z505HAED"]["written_bytes"]
        week = {"now": NOW - 7 * DAY, "host": "pve2",
                "disks": {"Z505HAED": {"written_bytes": hdd_now - 205e9}}}
        day = {"now": NOW - DAY, "host": "pve2",
               "disks": {"Z505HAED": {"written_bytes": hdd_now - 200e9}}}
        self.assertEqual(dhm.evaluate(facts(), ack=ack, history=[week, day]), [])

    def test_hdd_sustained_weekly_overage_still_fires(self):
        ack = {**SDA_ACK, **SDB_ACK}
        cur = {d["serial"]: d for d in dhm.summarise(facts())}
        hdd_now = cur["Z505HAED"]["written_bytes"]
        week = {"now": NOW - 7 * DAY, "host": "pve2",
                "disks": {"Z505HAED": {"written_bytes": hdd_now - 1200e9}}}
        probs = dhm.evaluate(facts(), ack=ack, history=[week])
        self.assertEqual(len(probs), 1)
        self.assertIn("Z505HAED", probs[0])
        self.assertIn("wrote 171 GB/day", probs[0])

    def test_nvme_media_errors_and_wear(self):
        nv = {"smartctl": {"exit_status": 0}, "serial_number": "NV1", "model_name": "Some NVMe",
              "device": {"protocol": "NVMe"},
              "nvme_smart_health_information_log": {"media_errors": 2, "percentage_used": 95,
                                                    "data_units_written": 1000, "critical_warning": 0}}
        f = {"host": "x", "now": NOW, "kernel_errors": [], "disks": [{"name": "nvme1n1", "tran": "nvme", "smart": nv}]}
        probs = dhm.evaluate(f, ack={}, history=[])
        self.assertTrue(any("media_errors=2" in p for p in probs), probs)
        self.assertTrue(any("95% of rated life used" in p for p in probs), probs)


class MainTests(unittest.TestCase):
    def _main(self, run, ack, hist_text="", off=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        ackp, histp = Path(d) / "ack.json", Path(d) / "h.jsonl"
        ackp.write_text(json.dumps(ack))
        histp.write_text(hist_text)
        err, out = io.StringIO(), io.StringIO()
        self.reported = []
        with mock.patch.object(dhm.subprocess, "run", run), \
                mock.patch.object(dhm.tailnet_self, "off_tailnet", return_value=off), \
                mock.patch.object(dhm.tailnet_self, "report_off_tailnet",
                                  side_effect=lambda *a, **k: self.reported.append(a)), \
                mock.patch.object(dhm.tailnet_self, "note_reachable"), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = dhm.main(["--ack", str(ackp), "--history", str(histp)])
        return rc, out.getvalue(), err.getvalue(), histp

    def test_off_tailnet_reports_and_does_not_fail(self):
        """T-5390: 2026-10-23 09:34Z this unit paged a false unit-failure during T-5381,
        quoting the previous run's SMART lines as if they were the problem."""
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "timed out"))
        rc, _, err, histp = self._main(run, {}, off="NeedsLogin")
        self.assertEqual(rc, 0)
        self.assertNotIn("FAIL", err)
        self.assertEqual([r[0] for r in self.reported], ["pve2-disk-health"])
        self.assertEqual(histp.read_text(), "", "no history row from a run that saw nothing")

    def test_ok_run_prints_ratios_and_appends_history(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(facts()), ""))
        rc, out, err, histp = self._main(run, {**SDA_ACK, **SDB_ACK})
        self.assertEqual(rc, 0, err)
        self.assertIn("Z505HAED", out)
        self.assertIn("life 87%", out)
        rows = [json.loads(l) for l in histp.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertIn("W1D200FL", rows[0]["disks"])
        self.assertIn("import json", run.call_args.kwargs["input"])

    def test_unreachable_host_fails_closed_and_writes_no_history(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "no route"))
        rc, _, err, histp = self._main(run, {})
        self.assertEqual(rc, 1)
        self.assertIn("could not read disks on pve2", err)
        self.assertEqual(histp.read_text(), "")

    def test_excluded_serial_is_never_queried(self):
        self.assertIn("S75YNF0X238969", dhm.EXCLUDE_SERIALS)
        self.assertIn("S75YNF0X238969", dhm.remote_script())


if __name__ == "__main__":
    unittest.main()
