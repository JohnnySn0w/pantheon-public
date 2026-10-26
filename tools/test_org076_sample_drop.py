"""ORG-076 BT3a seat side: the quota-probe sample DROP writer (T-4720, recovery constraint #1).

Hermetic: tmp drop dir only. The seat never writes a store/clone and never imports episode_store.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import quota_probe_detector as qpd

BINDING = SimpleNamespace(persona="recovery", account="claude-fallback")
T0 = datetime(2026, 10, 8, 1, 2, 3, 456789, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


class DropWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="drop-")
        self.addCleanup(self.tmp.cleanup)
        self.drop = Path(self.tmp.name)

    def files(self):
        return sorted(p.name for p in self.drop.iterdir())

    def test_writes_one_complete_file_group_readable(self):
        old = os.umask(0o077)  # a hostile seat umask must not matter
        try:
            name = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.PROBE_ERROR, "20260808T000000Z", "boom"),
                            drop=self.drop, now=T0)
        finally:
            os.umask(old)
        self.assertEqual(self.files(), [name])
        p = self.drop / name
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o664)
        body = json.loads(p.read_text())
        self.assertEqual((body["schema"], body["kind"], body["persona"], body["account"], body["outcome_kind"],
                          body["run_id"], body["detail"]),
                         (1, "quota-probe-sample", "recovery", "claude-fallback", "probe-error",
                          "20260808T000000Z", "boom"))
        self.assertTrue(body["sample_id"] and body["sampled_at"].endswith("Z"))

    def test_no_temp_file_left_behind(self):
        qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, None, ""), drop=self.drop, now=T0)
        self.assertFalse([n for n in self.files() if n.startswith(".")])

    def test_names_sort_chronologically(self):
        later = datetime(2026, 10, 8, 1, 2, 4, tzinfo=timezone.utc)
        b = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, "r2", ""), drop=self.drop, now=later)
        a = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, "r1", ""), drop=self.drop, now=T0)
        self.assertEqual(self.files(), [a, b])

    def test_never_overwrites_an_existing_file(self):
        with mock.patch.object(qpd.secrets, "token_hex", side_effect=["aaaaaa", "bbbbbb", "cccccc",
                                                                       "dddddd", "eeeeee", "ffffff"]):
            n1 = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, "r", ""), drop=self.drop, now=T0)
        (self.drop / n1).write_text("PRECIOUS")
        # force the next writer to collide on the first name candidate
        real = os.link
        calls = []

        def link(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 1:
                return real(src, self.drop / n1)  # raises FileExistsError
            return real(src, dst, *a, **k)

        with mock.patch.object(qpd.os, "link", link):
            n2 = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, "r", ""), drop=self.drop, now=T0)
        self.assertNotEqual(n1, n2)
        self.assertEqual((self.drop / n1).read_text(), "PRECIOUS")

    def test_concurrent_writers_all_land_complete(self):
        errs = []

        def w(i):
            try:
                qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, f"r{i}", ""), drop=self.drop, now=T0)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(16)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        names = self.files()
        self.assertEqual(len(names), 16)
        for n in names:
            json.loads((self.drop / n).read_text())

    def test_missing_drop_dir_is_not_created_and_feed_safely_swallows(self):
        gone = self.drop / "nope"
        with self.assertRaises(OSError):
            qpd.feed(BINDING, qpd.ProbeOutcome(qpd.READ, "r", ""), drop=gone, now=T0)
        self.assertFalse(gone.exists())
        self.assertIsNone(qpd.feed_safely(BINDING, qpd.ProbeOutcome(qpd.READ, "r", ""), drop=gone))

    def test_env_override_and_default(self):
        with mock.patch.dict(os.environ, {qpd.ENV_DROP_DIR: str(self.drop)}):
            self.assertEqual(qpd.drop_dir(), self.drop)
        env = {k: v for k, v in os.environ.items() if k != qpd.ENV_DROP_DIR}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(qpd.drop_dir(), Path("/var/lib/pantheon/org076/samples"))
        self.assertEqual(qpd.drop_dir("/x"), Path("/x"))

    def test_bad_persona_and_kind_refused(self):
        with self.assertRaises(ValueError):
            qpd.feed(SimpleNamespace(persona="../x", account="a"), qpd.ProbeOutcome(qpd.READ, "r", ""),
                     drop=self.drop)
        with self.assertRaises(ValueError):
            qpd.feed(BINDING, qpd.ProbeOutcome("nonsense", "r", ""), drop=self.drop)
        self.assertEqual(self.files(), [])

    def test_detail_is_bounded(self):
        n = qpd.feed(BINDING, qpd.ProbeOutcome(qpd.PROBE_ERROR, "r", "x" * 5000), drop=self.drop, now=T0)
        self.assertEqual(len(json.loads((self.drop / n).read_text())["detail"]), qpd.DETAIL_LIMIT)

    def test_seat_side_never_imports_the_store_and_writes_nothing_else(self):
        home = tempfile.mkdtemp(prefix="seat-home-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", home]))
        code = ("import sys, tempfile; from pathlib import Path; from types import SimpleNamespace;"
                "from tools import quota_probe_detector as q;"
                f"q.feed(SimpleNamespace(persona='recovery', account='a'), q.ProbeOutcome(q.READ,'r',''), drop=Path({str(self.drop)!r}));"
                "print('tools.episode_store' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(ROOT), "HOME": home})
        self.assertEqual(r.stdout.strip(), "False", r.stderr)
        self.assertEqual(os.listdir(home), [])


if __name__ == "__main__":
    unittest.main()
