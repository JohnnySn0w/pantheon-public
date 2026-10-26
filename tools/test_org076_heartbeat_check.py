"""ORG-076 BT3b: external-witness heartbeat reader contract + RFC-004 acceptance 6.9 kill-injection
(T-4709). The kill test runs a REAL runner cycle in a REAL subprocess and delivers a REAL SIGKILL."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tools import org076_heartbeat_check as hc
from tools import quota_probe_detector as qpd
from tools._episode_fixtures import RepoFixture

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "tools" / "org076_heartbeat_check.py"
NOW = 1_800_000_000


def write_hb(path: Path, **over):
    hb = {"agent": hc.AGENT, "ts": "2026-10-08T00:00:00Z", "epoch": NOW - 10, "pid": 123,
          "source": "episode_store", "schema_version": 1}
    hb.update(over)
    path.write_text(json.dumps(hb))
    return hb


class ReaderCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "hb.json"

    def chk(self, **kw):
        return hc.check(str(self.p), kw.get("max", 1200), kw.get("now", NOW))

    def test_fresh(self):
        write_hb(self.p)
        r = self.chk()
        self.assertEqual((r["status"], r["exit"], r["age_secs"], r["pid"]), ("fresh", 0, 10, 123))

    def test_boundary_age(self):
        write_hb(self.p, epoch=NOW - 1200)
        self.assertEqual(self.chk()["exit"], 0)
        write_hb(self.p, epoch=NOW - 1201)
        self.assertEqual((self.chk()["status"], self.chk()["exit"]), ("stale", 10))

    def test_missing(self):
        r = self.chk()
        self.assertEqual((r["status"], r["exit"]), ("missing", 11))

    def test_unreadable(self):
        write_hb(self.p)
        os.chmod(self.p, 0)
        if os.access(self.p, os.R_OK):
            self.skipTest("running as a user that ignores file modes")
        r = self.chk()
        self.assertEqual((r["status"], r["exit"]), ("unreadable", 12))

    def test_directory_in_place_of_file_is_unreadable(self):
        self.p.mkdir()
        self.assertEqual(self.chk()["exit"], 12)

    def test_malformed_shapes(self):
        for body in ("{not json", "[]", json.dumps({"agent": hc.AGENT, "epoch": "1", "schema_version": 1}),
                     json.dumps({"agent": hc.AGENT, "epoch": True, "schema_version": 1}),
                     json.dumps({"agent": "someone-else", "epoch": NOW, "schema_version": 1}),
                     json.dumps({"agent": hc.AGENT, "epoch": NOW, "schema_version": 2}), "\xff"):
            with self.subTest(body=body):
                self.p.write_bytes(body.encode("latin-1"))
                r = self.chk()
                self.assertEqual((r["status"], r["exit"]), ("malformed", 13))

    def test_future_dated_is_malformed_not_fresh(self):
        write_hb(self.p, epoch=NOW + 3600)
        self.assertEqual(self.chk()["exit"], 13)
        write_hb(self.p, epoch=NOW + 60)  # inside the skew tolerance
        self.assertEqual(self.chk()["exit"], 0)

    def test_cli_prints_one_json_line_and_exits_with_the_code(self):
        write_hb(self.p, epoch=NOW - 5000)
        r = subprocess.run([sys.executable, "-I", str(READER), "--path", str(self.p), "--now", str(NOW)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 10)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertEqual(json.loads(r.stdout)["status"], "stale")

    def test_reader_is_stdlib_only(self):
        src = READER.read_text()
        self.assertNotIn("from tools", src)
        self.assertNotIn("import tools", src)

    def test_default_path_and_threshold_are_the_documented_ones(self):
        self.assertEqual(hc.DEFAULT_PATH, "/var/lib/pantheon/org076/heartbeat/org076-episode-store.json")
        self.assertEqual(hc.DEFAULT_MAX_AGE_SECS, 1200)
        timer = (ROOT / "tools/systemd/pantheon-org076-store-runner.timer").read_text()
        service = (ROOT / "tools/systemd/pantheon-org076-store-runner.service").read_text()
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("TimeoutStartSec=10min", service)
        self.assertGreater(hc.DEFAULT_MAX_AGE_SECS, (5 + 10) * 60 - 300)  # > one hung cycle + a period


_CHILD = textwrap.dedent("""
    import json, os, sys, time
    from pathlib import Path
    from tools import episode_store as es
    from tools import org076_store_runner as run
    cfg_d = json.loads(sys.argv[1])
    cfg = run.Config(clone=Path(cfg_d["clone"]), drop=Path(cfg_d["drop"]), state_dir=Path(cfg_d["state"]),
                     heartbeat=Path(cfg_d["hb"]))
    ports = es.Ports(repo=cfg.clone, page=lambda s, b: None, mirror_url=None, heartbeat_path=cfg.heartbeat)
    def hook(point):
        Path(cfg_d["ready"]).write_text(point)   # the heartbeat was written by observe() just before
        time.sleep(300)                          # hold the cycle open until the parent SIGKILLs us
    run.run_cycle(cfg, ports, kill_hook=hook)
""")


class KillInjection(unittest.TestCase):
    """RFC-004 acceptance 6.9: kill the episode-store process; the external reader observes staleness."""

    def test_sigkill_of_a_real_runner_cycle_makes_the_reader_go_stale(self):
        with RepoFixture() as fx, tempfile.TemporaryDirectory() as td:
            root = Path(td)
            drop, state, hb, ready = root / "samples", root / "state", root / "hb" / "hb.json", root / "ready"
            drop.mkdir()
            from types import SimpleNamespace
            qpd.feed(SimpleNamespace(persona="recovery", account="claude-fallback"),
                     qpd.ProbeOutcome(qpd.READ, "20261008T000000Z", "d"), drop=drop)
            cfg = json.dumps({"clone": str(fx.repo), "drop": str(drop), "state": str(state), "hb": str(hb),
                              "ready": str(ready)})
            proc = subprocess.Popen([sys.executable, "-c", _CHILD, cfg], cwd=ROOT, env=fx.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 60
                while not ready.exists() and time.time() < deadline and proc.poll() is None:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), (proc.poll(), proc.stderr.read() if proc.poll() is not None else b""))
                alive = hc.check(str(hb), 1200, time.time())
                self.assertEqual((alive["status"], alive["pid"]), ("fresh", proc.pid))
                os.kill(proc.pid, signal.SIGKILL)       # real kill of the store process
                proc.wait(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            epoch = alive["epoch"]
            # the file survives the kill; time passing is the only thing that changes -> stale
            just = subprocess.run([sys.executable, "-I", str(READER), "--path", str(hb), "--now", str(epoch + 60)],
                                  capture_output=True, text=True)
            late = subprocess.run([sys.executable, "-I", str(READER), "--path", str(hb), "--now", str(epoch + 1500)],
                                  capture_output=True, text=True)
            self.assertEqual(just.returncode, 0)
            self.assertEqual(late.returncode, 10, late.stdout)
            self.assertEqual(json.loads(late.stdout)["status"], "stale")
            self.assertEqual(oct(hb.stat().st_mode & 0o777), "0o644")  # readable cross-account


if __name__ == "__main__":
    unittest.main()
