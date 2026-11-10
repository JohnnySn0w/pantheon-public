"""reboot_safety tests (T-1049): per-repo classification, the fleet verdict, and
main() exit codes. The `run` seam is stubbed — no live sudo/git."""

from __future__ import annotations

import unittest
from unittest import mock

from pathlib import Path

from tools import reboot_safety as rs

_MODAL_FIXTURE = Path(__file__).resolve().parent / "testdata" / "orchestrator_spend_limit_modal_pane.txt"


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


class CheckContinuity(unittest.TestCase):
    def test_live_pane_is_ok(self):
        r = rs.check_continuity("mason", "u", "pane:0",
                                run=lambda *_a, **_k: (0, "normal prompt", True))
        self.assertEqual(r["status"], rs.OK)

    def test_missing_pane_is_fail_closed(self):
        r = rs.check_continuity("mason", "u", "pane:0",
                                run=lambda *_a, **_k: (1, "", True))
        self.assertEqual(r["status"], rs.FAIL)
        self.assertIn("nonresponsive", r["detail"])

    def test_empty_capture_is_fail_closed(self):
        # T-6297: rc 0 with nothing captured proves nothing about the seat.
        for out in ("", "\n\n", "   \n"):
            with self.subTest(out=out):
                r = rs.check_continuity("mason", "u", "pane:0",
                                        run=lambda *_a, _o=out, **_k: (0, _o, True))
                self.assertEqual(r["status"], rs.FAIL)
                self.assertIn("unreadable", r["detail"])

    def test_runner_failure_is_fail_closed(self):
        r = rs.check_continuity("artisan", "u", "pane:0",
                                run=lambda *_a, **_k: (None, "", False))
        self.assertEqual(r["status"], rs.FAIL)
        self.assertIn("unchecked", r["detail"])

    def test_live_spend_modal_is_explicit_fail(self):
        # The real /rate-limit-options modal (captured orchestrator pane): footer is the last line.
        pane = _MODAL_FIXTURE.read_text(encoding="utf-8")
        r = rs.check_continuity("mason", "u", "pane:0", run=lambda *_a, **_k: (0, pane, True))
        self.assertEqual(r["status"], rs.FAIL)
        self.assertIn("capped", r["detail"])

    def test_cap_words_in_prose_above_the_prompt_are_warn_not_fail(self):
        # T-6288: an idle Artisan whose summary MENTIONED the spend limit was FAILed as capped.
        pane = (
            "⏺ Mason's post-reset monthly spend limit modal is cleared; usage limit reached\n"
            "  earlier is resolved and the seat is idle.\n\n"
            "╭──────────────────────────────────────────╮\n"
            "│ >                                        │\n"
            "╰──────────────────────────────────────────╯\n"
            "  ? for shortcuts\n"
        )
        r = rs.check_continuity("artisan", "u", "pane:0", run=lambda *_a, **_k: (0, pane, True))
        self.assertEqual(r["status"], rs.WARN)
        self.assertNotIn("capped", r["detail"])

    def test_cap_words_fail_closed_without_the_modal_classifier(self):
        with mock.patch.object(rs, "cap_modal_reaper", None):
            r = rs.check_continuity("artisan", "u", "pane:0",
                                    run=lambda *_a, **_k: (0, "usage limit reached", True))
        self.assertEqual(r["status"], rs.FAIL)

    def test_unpushed_is_warn(self):
        r = rs.check_repo("codex", "u", "/p", run=_stub(status_out="", ahead="3"))
        self.assertEqual(r["status"], rs.WARN)
        self.assertIn("3 local commit", r["detail"])
        self.assertIn("not on origin", r["detail"])

    def test_no_upstream_is_warn_unknown(self):
        r = rs.check_repo("recovery", "u", "/p", run=_stub(status_out="", ahead="?"))
        self.assertEqual(r["status"], rs.WARN)
        self.assertIn("unknown", r["detail"])

    def test_unreadable_repo_is_fail_closed(self):
        r = rs.check_repo("orchestrator", "u", "/p", run=_stub(status_ran=False))
        self.assertEqual(r["status"], rs.FAIL)

    def test_dirty_takes_precedence_over_ahead(self):
        # a dirty tree should FAIL even if it would also be ahead
        r = rs.check_repo("recovery", "u", "/p",
                          run=_stub(status_out=" M x\n", ahead="5"))
        self.assertEqual(r["status"], rs.FAIL)


class RenderAndMain(unittest.TestCase):
    def test_registry_covers_all_current_seats(self):
        self.assertEqual({agent for agent, _user, _path in rs.REPOS}, {
            "recovery", "orchestrator", "specialist", "codex",
            "archivist", "mason", "artisan", "watchman",
        })

    def test_continuity_registry_has_exact_all_eight_parity(self):
        expected = {
            "recovery", "orchestrator", "specialist", "codex",
            "archivist", "mason", "artisan", "watchman",
        }
        agents = [agent for agent, _user, _pane in rs.CONTINUITY]
        self.assertEqual(set(agents), expected)
        self.assertEqual(len(agents), len(expected))

    def test_retired_codex_config_repo_is_not_reintroduced(self):
        paths = {path for _agent, _user, path in rs.REPOS}
        self.assertNotIn("/home/codex/codex-config", paths)

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
