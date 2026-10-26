"""Tests for tools/pve2_backup_freshness.py (T-4998). Run: python3 -m unittest tools.test_pve2_backup_freshness"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from tools import pve2_backup_freshness as pbf

NOW = 1_800_000_000.0
H = 3600


def facts(**over):
    f = {"now": NOW,
         "guests": [{"vmid": 200, "name": "forgejo", "template": 0},
                    {"vmid": 210, "name": "gpu-ml", "template": 0},
                    {"vmid": 9000, "name": "tmpl", "template": 1}],
         "dumps": {"200": {"path": "a", "mtime": NOW - 10 * H, "log_finished": True, "log_error": False},
                   "210": {"path": "b", "mtime": NOW - 10 * H, "log_finished": True, "log_error": False}},
         "gitdata_mtime": NOW - 9 * H}
    f.update(over)
    return f


class EvaluateTests(unittest.TestCase):
    def test_fresh_and_clean_is_ok_and_templates_skipped(self):
        self.assertEqual(pbf.evaluate(facts()), [])

    def test_stale_dump(self):
        f = facts()
        f["dumps"]["210"]["mtime"] = NOW - 180 * H
        self.assertEqual(pbf.evaluate(f), ["guest 210 (gpu-ml): newest dump is 180h old (limit 174h)"])

    def test_weekly_guest_dumps_a_day_or_two_old_are_fine(self):
        """T-5390: 2026-10-23 14:51Z the 30h limit flagged every guest at 31-32h, the
        day after T-5356 made the job weekly. The git-data copy stays daily."""
        f = facts()
        for d in f["dumps"].values():
            d["mtime"] = NOW - 32 * H
        self.assertEqual(pbf.evaluate(f), [])
        self.assertEqual(pbf.evaluate(facts(gitdata_mtime=NOW - 31 * H)),
                         ["git-data: newest archive is 31h old (limit 30h)"])

    def test_limits_match_the_weekly_schedule(self):
        self.assertEqual(pbf.DEFAULT_MAX_AGE_HOURS, 174.0)
        self.assertEqual(pbf.DEFAULT_GITDATA_MAX_AGE_HOURS, 30.0)

    def test_missing_dump_and_missing_log(self):
        f = facts()
        del f["dumps"]["200"]
        f["dumps"]["210"]["log_finished"] = None
        self.assertEqual(pbf.evaluate(f), ["guest 200 (forgejo): no dump in archive at all",
                                           "guest 210 (gpu-ml): newest dump has no log"])

    def test_error_in_log_or_no_finished_line(self):
        f = facts()
        f["dumps"]["200"]["log_error"] = True
        f["dumps"]["210"]["log_finished"] = False
        self.assertEqual(pbf.evaluate(f), ["guest 200 (forgejo): newest dump log shows ERROR",
                                           "guest 210 (gpu-ml): newest dump log shows no Finished Backup line"])

    def test_gitdata_skipped_when_host_has_no_gitdata_copy(self):
        self.assertEqual(pbf.evaluate(facts(gitdata_mtime=None), check_gitdata=False), [])

    def test_gitdata_missing_or_stale(self):
        self.assertEqual(pbf.evaluate(facts(gitdata_mtime=None)),
                         ["git-data: no archive at all in /mnt/archive/git-data"])
        self.assertEqual(pbf.evaluate(facts(gitdata_mtime=NOW - 31 * H)),
                         ["git-data: newest archive is 31h old (limit 30h)"])


class MainTests(unittest.TestCase):
    def _main(self, run, off=None):
        err, out = io.StringIO(), io.StringIO()
        self.reported = []
        with mock.patch.object(pbf.subprocess, "run", run), \
                mock.patch.object(pbf.tailnet_self, "off_tailnet", return_value=off), \
                mock.patch.object(pbf.tailnet_self, "report_off_tailnet",
                                  side_effect=lambda *a, **k: self.reported.append(a)), \
                mock.patch.object(pbf.tailnet_self, "note_reachable"), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = pbf.main([])
        return rc, out.getvalue(), err.getvalue()

    def test_off_tailnet_reports_once_per_host_and_does_not_fail(self):
        """T-5390: 2026-10-23 this unit paged a false unit-failure during T-5381."""
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "timed out"))
        rc, _, err = self._main(run, off="NeedsLogin")
        self.assertEqual(rc, 0)
        self.assertNotIn("FAIL", err)
        self.assertEqual([r[0] for r in self.reported], ["pve2-backup-freshness"] * 2)

    def test_unknown_tailnet_state_still_fails_closed(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "timed out"))
        rc, _, err = self._main(run, off=None)
        self.assertEqual(rc, 1)
        self.assertIn("could not read backups", err)

    def test_unreachable_host_fails_closed(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 255, "", "no route"))
        rc, _, err = self._main(run)
        self.assertEqual(rc, 1)
        self.assertIn("could not read backups", err)

    def test_ok_path_checks_every_host_and_feeds_script_on_stdin(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(facts()), ""))
        rc, out, _ = self._main(run)
        self.assertEqual(rc, 0)
        self.assertIn("pve2: OK 2 guest(s) + git-data", out)
        self.assertIn("boxon: OK 2 guest(s) fresh", out)
        hosts = [c.args[0][-2] for c in run.call_args_list]
        self.assertEqual(hosts, ["pve2", "boxon"])
        for c in run.call_args_list:
            self.assertIn("import glob", c.kwargs["input"])
            self.assertTrue(c.args[0][-1].endswith("python3 -"))
        self.assertIn("'/mnt/pve/pve2-archive/dump'", run.call_args_list[1].kwargs["input"])

    def test_one_host_unreachable_still_checks_the_other(self):
        ok = subprocess.CompletedProcess([], 0, json.dumps(facts()), "")
        down = subprocess.CompletedProcess([], 255, "", "no route")
        run = mock.Mock(side_effect=[ok, down])
        rc, out, err = self._main(run)
        self.assertEqual(rc, 1)
        self.assertIn("pve2: OK", out)
        self.assertIn("could not read backups on boxon", err)

    def test_problem_lines_name_the_host(self):
        f = facts()
        del f["dumps"]["200"]
        ok = subprocess.CompletedProcess([], 0, json.dumps(facts()), "")
        bad = subprocess.CompletedProcess([], 0, json.dumps(f), "")
        rc, _, err = self._main(mock.Mock(side_effect=[ok, bad]))
        self.assertEqual(rc, 1)
        self.assertIn("FAIL boxon: guest 200 (forgejo): no dump in archive at all", err)


if __name__ == "__main__":
    unittest.main()
