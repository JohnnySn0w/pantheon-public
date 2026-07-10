"""reboot_safety tests (T-1049): per-repo classification, the fleet verdict, and
main() exit codes. The `run` seam is stubbed — no live sudo/git."""

from __future__ import annotations

import unittest
from unittest import mock

from tools import reboot_safety as rs


def _stub(*, status_out="", status_rc=0, status_ran=True, ahead="0", ahead_ran=True):
    def _f(user, argv_str, timeout=20):
        if "status --porcelain" in argv_str:
            return (status_rc, status_out, status_ran)
        if "rev-list" in argv_str:
            return (0, ahead + "\n", ahead_ran)
        return (0, "", True)
    return _f


class CheckRepo(unittest.TestCase):
    def test_clean_and_pushed_is_ok(self):
        r = rs.check_repo("recovery", "u", "/p", run=_stub(status_out="", ahead="0"))
        self.assertEqual(r["status"], rs.OK)

    def test_dirty_is_fail(self):
        r = rs.check_repo("recovery", "u", "/p",
                          run=_stub(status_out=" M a.py\n?? new.txt\n"))
        self.assertEqual(r["status"], rs.FAIL)
        self.assertIn("2 uncommitted", r["detail"])

    def test_unpushed_is_warn(self):
        r = rs.check_repo("codex", "u", "/p", run=_stub(status_out="", ahead="3"))
        self.assertEqual(r["status"], rs.WARN)
        self.assertIn("3 local commit", r["detail"])
        self.assertIn("not on origin", r["detail"])

    def test_no_upstream_is_warn_unknown(self):
        r = rs.check_repo("recovery", "u", "/p", run=_stub(status_out="", ahead="?"))
        self.assertEqual(r["status"], rs.WARN)
        self.assertIn("unknown", r["detail"])

    def test_unreadable_repo_is_warn_not_fail(self):
        r = rs.check_repo("orchestrator", "u", "/p", run=_stub(status_ran=False))
        self.assertEqual(r["status"], rs.WARN)

    def test_dirty_takes_precedence_over_ahead(self):
        # a dirty tree should FAIL even if it would also be ahead
        r = rs.check_repo("recovery", "u", "/p",
                          run=_stub(status_out=" M x\n", ahead="5"))
        self.assertEqual(r["status"], rs.FAIL)


class RenderAndMain(unittest.TestCase):
    def test_render_all_safe(self):
        results = [{"agent": "recovery", "repo": "/p", "status": rs.OK, "detail": "x"}]
        self.assertIn("SAFE", rs.render(results))

    def test_render_dirty_blocks(self):
        results = [{"agent": "recovery", "repo": "/p", "status": rs.FAIL,
                    "detail": "1 uncommitted"}]
        out = rs.render(results)
        self.assertIn("NOT SAFE TO REBOOT", out)
        self.assertIn("/p", out)

    def test_render_unpushed_warns(self):
        results = [{"agent": "codex", "repo": "/c", "status": rs.WARN,
                    "detail": "clean, but 2 local commit(s) not on origin"}]
        self.assertIn("UNPUSHED", rs.render(results))

    def test_main_exit_0_when_safe(self):
        with mock.patch.object(rs, "sweep",
                               return_value=[{"agent": "recovery", "repo": "/p",
                                              "status": rs.OK, "detail": ""}]):
            self.assertEqual(rs.main([]), 0)

    def test_main_exit_1_when_dirty(self):
        with mock.patch.object(rs, "sweep",
                               return_value=[{"agent": "recovery", "repo": "/p",
                                              "status": rs.FAIL, "detail": ""}]):
            self.assertEqual(rs.main([]), 1)

    def test_main_exit_0_when_only_unpushed(self):
        # unpushed is a WARN, not a reboot blocker
        with mock.patch.object(rs, "sweep",
                               return_value=[{"agent": "codex", "repo": "/c",
                                              "status": rs.WARN, "detail": "not on origin"}]):
            self.assertEqual(rs.main([]), 0)


if __name__ == "__main__":
    unittest.main()
