"""Tests for tools/s3star_seat_evidence.py (T-4928). Run: python3 -m unittest tools.test_s3star_seat_evidence"""
from __future__ import annotations

import getpass
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools import s3star_seat_evidence as sse

ME = getpass.getuser()
NOW = datetime(2026, 10, 20, 8, 15, tzinfo=timezone.utc)


def fake_run(fail_on: str | None = None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if fail_on and any(fail_on in a for a in argv):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Failed to connect to bus\nmore")
        return subprocess.CompletedProcess(argv, 0, stdout=f"out of {' '.join(argv[-3:])}\n", stderr="")
    return run, calls


class CollectTests(unittest.TestCase):
    def test_four_captures_per_account_run_as_that_account(self):
        run, calls = fake_run()
        caps = sse.collect([ME, "root"], run=run)
        self.assertEqual([c.name for c in caps][:4],
                         [f"units-{ME}", f"timers-{ME}", f"failed-{ME}", f"transcripts-{ME}"])
        self.assertTrue(all(c.status == "OK" for c in caps))
        # own account runs directly; any other goes through sudo -n -u <acct> (seat_argv)
        self.assertNotEqual(calls[0][0], "sudo")
        self.assertEqual(calls[4][:4], ["sudo", "-n", "-u", "root"])

    def test_transcript_capture_is_inventory_only(self):
        _, _, argv = sse.capture_commands(ME, 8)[3]
        script = argv[-1]
        self.assertIn("-printf '%p\\t%s\\t", script)
        for reader in ("cat ", "head ", "tail ", "jq ", "grep "):
            self.assertNotIn(reader, script)

    def test_failed_capture_is_recorded_never_dropped(self):
        run, _ = fake_run(fail_on="list-timers")
        caps = sse.collect([ME], run=run)
        timers = next(c for c in caps if c.name == f"timers-{ME}")
        self.assertEqual(timers.status, "FAILED (exit 1)")
        self.assertEqual(timers.body, "Failed to connect to bus")

    def test_unknown_account_is_a_failed_row(self):
        run, _ = fake_run()
        caps = sse.collect(["no-such-account-t4928"], run=run)
        self.assertEqual([(c.name, c.status) for c in caps],
                         [("account-no-such-account-t4928", "FAILED (no such account)")])


class BundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, fail_on=None, now=NOW):
        run, _ = fake_run(fail_on)
        return sse.write_bundle(sse.collect([ME], run=run), self.root, now=now, days=8, captured_by=ME)

    def test_bundle_files_immutable_manifest_hashed_latest_linked(self):
        out = self.write()
        self.assertEqual(out, self.root / "seat-evidence" / "2026-10-20")
        self.assertEqual(os.readlink(self.root / "seat-evidence" / "latest"), "2026-10-20")
        files = sorted(p.name for p in out.iterdir())
        self.assertEqual(len(files), 5)
        for p in out.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o444)
        m = (out / "MANIFEST.md").read_text()
        self.assertIn("generated_at: 2026-10-20T08:15:00Z", m)
        self.assertIn("Failed captures: 0", m)

    def test_failed_rows_show_in_manifest_and_file(self):
        out = self.write(fail_on="list-timers")
        m = (out / "MANIFEST.md").read_text()
        self.assertIn(f"timers-{ME}: FAILED (exit 1)", m)
        self.assertTrue((out / f"timers-{ME}.txt").read_text().startswith("FAILED (exit 1)"))

    def test_same_day_rerun_replaces_and_next_day_moves_latest(self):
        self.write(fail_on="list-timers")
        out = self.write()
        self.assertIn("Failed captures: 0", (out / "MANIFEST.md").read_text())
        self.write(now=datetime(2026, 10, 21, 8, 15, tzinfo=timezone.utc))
        base = self.root / "seat-evidence"
        self.assertEqual(os.readlink(base / "latest"), "2026-10-21")
        self.assertEqual(sorted(p.name for p in base.iterdir() if not p.name.startswith(".")),
                         ["2026-10-20", "2026-10-21", "latest"])
        self.assertEqual([p.name for p in base.iterdir() if p.name.startswith(".")], [])

    def test_main_exit_codes(self):
        run, _ = fake_run(fail_on="list-timers")
        real = sse.collect
        with mock.patch.object(sse, "collect", lambda accts, days: real([ME], run=run)), \
                mock.patch("builtins.print"):
            self.assertEqual(sse.main(["--workspace", str(self.root)]), 1)
            self.assertEqual(sse.main(["--workspace", "/proc/nope-t4928"]), 2)


if __name__ == "__main__":
    unittest.main()
