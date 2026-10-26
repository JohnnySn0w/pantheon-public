"""ORG-076 BT3b: in-band store-liveness meta-detector (T-4709). Hermetic: real bare origin, tmp
dirs, one shared fake clock, the real ticket.py filer."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools import episode_store as es
from tools import org076_meta as meta
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools.test_org076_reconciler import Rig as _Rig


class MetaRig(_Rig):
    def now(self):
        return self.clock._t.timestamp()

    def preset_meta(self, first_ago=7200, cycles=()):
        st = run._load_state(self.cfg)
        n = self.now()
        st["meta"] = {"first_cycle_at": n - first_ago, "cycles": [n - c for c in cycles]}
        run._save_state(self.cfg, st)

    def mdomain(self):
        return es.load_domain(meta.DOMAIN, self.state)

    def mepisodes(self):
        return [e for e in self.mdomain()["episodes"].values()]


class CycleRate(MetaRig):
    def test_fresh_install_warmup_is_silent(self):
        for _ in range(4):
            self.assertEqual(self.cycle().exit_code, 0)
        self.assertEqual(self.mepisodes(), [])
        st = run._load_state(self.cfg)
        self.assertEqual(len(st["meta"]["cycles"]), 4)

    def test_a_gap_opens_an_episode_at_n_3_and_files_one_ticket_routed_to_watchman(self):
        self.preset_meta(first_ago=7200, cycles=())  # long-running store, then a multi-hour gap
        for i in range(2):
            self.cycle()
            self.assertEqual(self.mepisodes(), [], f"cycle {i + 1}: below N")
        rep = self.cycle()  # third consecutive low-count cycle -> OPEN
        self.assertEqual(rep.exit_code, 0, rep.errors)
        (ep,) = self.mepisodes()
        self.assertEqual((ep["fault_id"], ep["verdict_kind"], ep["detector"]), ("cycle-rate-low", "broken", meta.CYCLE_RATE))
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), (es.EMITTED, es.VERIFIED))
        (rel,) = self.fx.origin_ticket_files()
        txt = self.origin_text(rel)
        self.assertIn("requester: recovery", txt)
        self.assertIn("repair_route: watchman@codex", txt)
        self.assertIn("fault_id: cycle-rate-low", txt)

    def test_recovery_closes_after_m_healthy_once_the_window_refills(self):
        self.preset_meta(first_ago=7200)
        for _ in range(3):
            self.cycle()
        (ep,) = self.mepisodes()
        self.preset_meta(first_ago=7200, cycles=[60 * i for i in range(1, 12)])  # a healthy hour of cycles
        for _ in range(2):
            self.cycle()
        (ep2,) = self.mepisodes()
        self.assertEqual((ep2["episode_id"], ep2["state"]), (ep["episode_id"], es.RESOLVED))

    def test_a_refused_cycle_is_not_a_completed_cycle(self):
        self.cycle()
        before = len(run._load_state(self.cfg)["meta"]["cycles"])
        (self.fx.repo / "stray.txt").write_text("x")
        rep = self.cycle()
        self.assertEqual(rep.exit_code, 3)
        self.assertEqual(len(run._load_state(self.cfg)["meta"]["cycles"]), before)

    def test_record_cycle_trims_to_window_and_caps(self):
        st: dict = {}
        for i in range(200):
            meta.record_cycle(st, 1000.0 + i)
        self.assertLessEqual(len(st["meta"]["cycles"]), meta.MAX_STORED)
        meta.record_cycle(st, 1000.0 + 10 * meta.WINDOW_SECS)
        self.assertEqual(st["meta"]["cycles"], [1000.0 + 10 * meta.WINDOW_SECS])
        self.assertEqual(st["meta"]["first_cycle_at"], 1000.0)

    def test_verdict_threshold_boundaries(self):
        n = 100000.0
        st = {"meta": {"first_cycle_at": n - 7200, "cycles": [n - i for i in range(meta.MIN_CYCLES)]}}
        self.assertIsInstance(meta.cycle_rate_verdict(st, n), es.Healthy)
        st["meta"]["cycles"].pop()
        self.assertIsInstance(meta.cycle_rate_verdict(st, n), es.Broken)


class HeartbeatReadback(MetaRig):
    def test_silently_failing_heartbeat_write_is_caught_and_opens_an_episode(self):
        blocked = self.root / "blocked"
        blocked.write_text("a FILE where the heartbeat dir should be: every write fails and is swallowed")
        self.cfg.heartbeat = blocked / "hb.json"
        for _ in range(3):
            self.cycle(heartbeat_path=self.cfg.heartbeat)
        (ep,) = [e for e in self.mepisodes() if e["detector"] == meta.HB_READBACK]
        self.assertEqual((ep["fault_id"], ep["verdict_kind"]), ("heartbeat-write-failing", "broken"))

    def test_healthy_heartbeat_is_healthy_and_stale_one_is_broken(self):
        hb = self.root / "x.json"
        hb.write_text(json.dumps({"epoch": 1000}))
        self.assertIsInstance(meta.heartbeat_verdict(hb, 900), es.Healthy)
        v = meta.heartbeat_verdict(hb, 2000)
        self.assertEqual((type(v), v.fault_id), (es.Broken, "heartbeat-write-failing"))
        hb.write_text("{nope")
        self.assertIsInstance(meta.heartbeat_verdict(hb, 0), es.Broken)
        self.assertIsInstance(meta.heartbeat_verdict(self.root / "gone.json", 0), es.Broken)

    def test_unreadable_is_unknown_never_healthy(self):
        d = self.root / "adir"
        d.mkdir()
        v = meta.heartbeat_verdict(d, 0)
        self.assertEqual((type(v), v.fault_id), (es.Unknown, "heartbeat-readback-unreadable"))


class IngestBacklog(MetaRig):
    def test_missing_dir_and_fresh_files_are_healthy(self):
        n = 1_800_000_000.0
        self.assertIsInstance(meta.backlog_verdict(self.root / "nope", n), es.Healthy)
        f = self.drop / "a__recovery__nr__1.json"
        f.write_text("{}")
        os.utime(f, (n - 60, n - 60))
        self.assertIsInstance(meta.backlog_verdict(self.drop, n), es.Healthy)

    def test_old_file_is_broken_and_hidden_or_rejected_files_do_not_count(self):
        n = 1_800_000_000.0
        (self.drop / "rejected").mkdir()
        for name in (".sample-tmp-1", "rejected/old.json"):
            p = self.drop / name
            p.write_text("{}")
            os.utime(p, (n - 99999, n - 99999))
        self.assertIsInstance(meta.backlog_verdict(self.drop, n), es.Healthy)
        f = self.drop / "a__recovery__nr__1.json"
        f.write_text("{}")
        os.utime(f, (n - meta.BACKLOG_SECS - 1, n - meta.BACKLOG_SECS - 1))
        v = meta.backlog_verdict(self.drop, n)
        self.assertEqual((type(v), v.fault_id), (es.Broken, "ingest-backlog"))

    def test_unreadable_dir_is_unknown(self):
        self.drop.chmod(0)
        try:
            if os.access(self.drop, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            v = meta.backlog_verdict(self.drop, 1.0)
            self.assertEqual((type(v), v.fault_id), (es.Unknown, "ingest-backlog-unmeasurable"))
        finally:
            self.drop.chmod(0o755)

    def test_held_backlog_opens_an_episode_through_the_runner(self):
        self.drop_sample(kind=qpd.READ)
        (f,) = [p for p in self.drop.iterdir() if p.is_file()]
        old = self.now() - 2 * meta.BACKLOG_SECS
        os.utime(f, (old, old))
        path = es.store_path(f"{qpd.DETECTOR}:recovery", self.state)
        with es._DomainLock(path, 5):  # the persona domain is busy: the file is held every cycle
            for _ in range(3):
                self.cycle(lock_timeout=0.05)
        (ep,) = [e for e in self.mepisodes() if e["detector"] == meta.BACKLOG]
        self.assertEqual(ep["fault_id"], "ingest-backlog")


class Limits(unittest.TestCase):
    def test_docstring_states_the_in_band_limit(self):
        self.assertIn("CANNOT see the store totally", meta.__doc__)
        self.assertIn("external witness", meta.__doc__)


if __name__ == "__main__":
    unittest.main()
