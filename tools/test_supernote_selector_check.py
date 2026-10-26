"""Tests for tools/supernote_selector_check.py (T-4593). Synthetic log lines only."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tools import supernote_selector_check as chk

NOW = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


def _diff(ts: str, n: int) -> str:
    return (
        f"[{ts}] diff: admitted=288 cache=274 stale_set=1 select=0 (new=0 stale/rewritten=0) "
        f"stale_no_png={n} mtime_rule=OFF(superseded by stale-set)"
    )


class TestAssess(unittest.TestCase):
    def test_clean_recent_run_is_ok(self) -> None:
        code, msg, _ = chk.assess(_diff("2026-10-06T05:17:03Z", 0), NOW)
        self.assertEqual(code, 0, msg)

    def test_flag_in_window_fails_with_the_count(self) -> None:
        log = "\n".join([
            _diff("2026-10-06T05:17:03Z", 2),
            "[2026-10-06T05:17:03Z] FLAG stale-set page(s) with NO P2 png: 2 (unrefreshable until re-rendered)",
        ])
        code, _, worst = chk.assess(log, NOW)
        self.assertEqual((code, worst), (1, 2))

    def test_flag_older_than_the_window_is_ignored(self) -> None:
        log = "\n".join([_diff("2026-10-04T05:17:03Z", 3), _diff("2026-10-06T05:17:03Z", 0)])
        self.assertEqual(chk.assess(log, NOW)[0], 0)

    def test_no_run_in_window_fails_closed(self) -> None:
        self.assertEqual(chk.assess(_diff("2026-10-04T05:17:03Z", 0), NOW)[0], 2)
        self.assertEqual(chk.assess("", NOW)[0], 2)

    def test_future_dated_run_is_not_fresh(self) -> None:
        """REBOUND-1: a line stamped after now must not keep the check green."""
        code, msg, _ = chk.assess(_diff("2026-10-09T05:17:03Z", 0), NOW)
        self.assertEqual(code, 2)
        self.assertIn("future-dated", msg)
        # small clock skew is tolerated
        self.assertEqual(chk.assess(_diff("2026-10-06T06:03:00Z", 0), NOW)[0], 0)

    def test_future_dated_flag_still_alerts(self) -> None:
        self.assertEqual(chk.assess(_diff("2026-10-09T05:17:03Z", 4), NOW)[:3:2], (1, 4))

    def test_pre_t4593_diff_lines_do_not_count_as_runs(self) -> None:
        old = "[2026-10-06T05:17:03Z] diff: admitted=288 cache=274 stale_set=0 select=0"
        self.assertEqual(chk.assess(old, NOW)[0], 2)


SECRET = "PSECRET20261006 dear diary"


class TestPipelineStageFailures(unittest.TestCase):
    """T-5045: a crashed ingest run (FAILED line) and OCR failures (FLAG ocr_failed) are visible."""

    def test_failed_line_in_window_is_code_4(self) -> None:
        log = "\n".join([_diff("2026-10-06T05:17:03Z", 0), "[2026-10-06T05:20:00Z] FAILED: RuntimeError('x')"])
        code, _, worst = chk.assess(log, NOW)
        self.assertEqual((code, worst), (4, 1))

    def test_failed_cmd_line_is_code_4(self) -> None:
        log = "\n".join([_diff("2026-10-06T05:17:03Z", 0), "[2026-10-06T05:20:00Z] FAILED cmd: rsync -t a b"])
        self.assertEqual(chk.assess(log, NOW)[0], 4)

    def test_failed_older_than_window_is_ignored(self) -> None:
        log = "\n".join(["[2026-10-04T05:20:00Z] FAILED: RuntimeError('x')", _diff("2026-10-06T05:17:03Z", 0)])
        self.assertEqual(chk.assess(log, NOW)[0], 0)

    def test_ocr_failed_with_clean_diff_is_code_5(self) -> None:
        log = "\n".join([
            "[2026-10-06T05:17:01Z] FLAG ocr_failed: 3 page(s) failed OCR this run (retried next drop)",
            _diff("2026-10-06T05:17:03Z", 0),
        ])
        code, msg, worst = chk.assess(log, NOW)
        self.assertEqual((code, worst), (5, 3))
        self.assertIn("3", msg)

    def test_no_ocr_failed_line_is_ok(self) -> None:
        self.assertEqual(chk.assess(_diff("2026-10-06T05:17:03Z", 0), NOW)[0], 0)

    def test_stale_flag_and_ocr_failed_is_code_1_naming_both(self) -> None:
        log = "\n".join([
            "[2026-10-06T05:17:01Z] FLAG ocr_failed: 3 page(s) failed OCR this run (retried next drop)",
            _diff("2026-10-06T05:17:03Z", 2),
        ])
        code, msg, worst = chk.assess(log, NOW)
        self.assertEqual((code, worst), (1, 2))
        self.assertIn("stale-set", msg)
        self.assertIn("ocr_failed", msg)

    def test_failed_and_stale_is_code_4_naming_both(self) -> None:
        log = "\n".join([_diff("2026-10-06T05:17:03Z", 2), "[2026-10-06T05:20:00Z] FAILED: RuntimeError('x')"])
        code, msg, _ = chk.assess(log, NOW)
        self.assertEqual(code, 4)
        self.assertIn("FAILED", msg)
        self.assertIn("stale-set", msg)

    def test_failed_with_no_diff_line_is_4_not_2(self) -> None:
        self.assertEqual(chk.assess("[2026-10-06T05:20:00Z] FAILED: RuntimeError('x')", NOW)[0], 4)

    def test_future_dated_failed_and_ocr_still_count(self) -> None:
        self.assertEqual(chk.assess("[2026-10-09T05:20:00Z] FAILED: x", NOW)[0], 4)
        fut = "[2026-10-09T05:17:01Z] FLAG ocr_failed: 2 page(s) failed OCR this run (retried next drop)"
        self.assertEqual(chk.assess(fut, NOW)[:3:2], (5, 2))

    def test_failed_body_text_never_reaches_msg_or_mail(self) -> None:
        log = f"[2026-10-06T05:20:00Z] FAILED: RuntimeError('{SECRET}')"
        code, msg, worst = chk.assess(log, NOW)
        self.assertEqual(code, 4)
        self.assertNotIn("PSECRET", msg)
        self.assertNotIn("diary", msg)
        sent = _capture_mail(lambda: chk.mail_archivist(worst, code, msg))
        self.assertEqual(len(sent), 1)   # else the loop below is vacuous
        for a, k in sent:
            blob = " ".join(map(str, a)) + " " + " ".join(map(str, k.values()))
            self.assertNotIn("PSECRET", blob)
            self.assertNotIn("diary", blob)

    def test_page_versions_held_is_code_6_for_each_kind(self) -> None:
        """T-5534: the ingest's exact FLAG lines (stamped by its log())."""
        for line, n in (("FLAG page-versions winner_flip_held=2", 2), ("FLAG page-versions reclassify_held=1", 1),
                        ("FLAG page-versions schema_unknown=1 (not the supernote-page-versions/1 family; held)", 1)):
            with self.subTest(line=line):
                log = "\n".join([f"[2026-10-06T05:17:01Z] {line}", _diff("2026-10-06T05:17:03Z", 0)])
                code, msg, worst = chk.assess(log, NOW)
                self.assertEqual((code, worst), (6, n))
                self.assertIn("page-versions", msg)

    def test_page_versions_held_ranks_below_ocr_failed_and_above_no_run(self) -> None:
        held = "[2026-10-06T05:17:01Z] FLAG page-versions winner_flip_held=1"
        ocr = "[2026-10-06T05:17:01Z] FLAG ocr_failed: 3 page(s) failed OCR this run (retried next drop)"
        both = chk.assess("\n".join([held, ocr, _diff("2026-10-06T05:17:03Z", 0)]), NOW)
        self.assertEqual(both[0], 5)
        self.assertIn("page-versions", both[1])   # still named in the message
        self.assertEqual(chk.assess(held, NOW)[0], 6)   # no diff line: a hold outranks "no run"
        old = "[2026-10-01T05:17:01Z] FLAG page-versions winner_flip_held=1"   # outside the window
        self.assertEqual(chk.assess("\n".join([old, _diff("2026-10-06T05:17:03Z", 0)]), NOW)[0], 0)

    def test_code_6_mails_a_re_pick_subject(self) -> None:
        sent = _capture_mail(lambda: chk.mail_archivist(2, 6, "page-versions: up to 2 element(s) held"))
        self.assertEqual(len(sent), 1)
        self.assertIn("code 6", sent[0][1]["subject"])
        self.assertIn("--allow-winner-change", sent[0][0][1])

    def test_codes_4_and_5_mail_distinct_subjects(self) -> None:
        def go() -> None:
            chk.mail_archivist(1, 4, "1 ingest run(s) FAILED")
            chk.mail_archivist(3, 5, "ocr_failed: 3 page(s)")
        sent = _capture_mail(go)
        s4, s5 = (k["subject"] for _, k in sent)
        self.assertNotEqual(s4, s5)
        self.assertIn("code 4", s4)
        self.assertIn("3", s5)
        self.assertIn("supernote-ingest.log", sent[0][0][1])
        self.assertIn("retried every drop", sent[1][0][1])
        self.assertIn("supernote-ingest.log", sent[1][0][1])


def _capture_mail(fn) -> list:
    sent: list = []
    fake = type("M", (), {"send": staticmethod(lambda *a, **k: sent.append((a, k)))})
    with patch.dict("sys.modules", {"tools.mailbox": fake}), \
         patch("tools.agent_role_map.mailbox_hub", return_value="/nonexistent-hub"):
        import tools
        with patch.object(tools, "mailbox", fake, create=True):
            fn()
    return sent


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        # main() consults swarm's own tailnet state on the failure path and records reachability on
        # success (T-5385); pin both so the tests never read this host's live tailscale or state file.
        from tools import tailnet_self

        p_off = patch.object(tailnet_self, "off_tailnet", return_value=None)
        p_ok = patch.object(tailnet_self, "note_reachable")
        self.off, self.reach = p_off.start(), p_ok.start()
        self.addCleanup(p_off.stop)
        self.addCleanup(p_ok.stop)

    def test_unreachable_host_fails_closed(self) -> None:
        with patch.object(chk, "fetch_log_tail", side_effect=ConnectionError("down")):
            self.assertEqual(chk.main(["--no-mail"]), 3)

    def test_off_tailnet_suppresses_code_3_and_mail(self) -> None:
        """T-5385: swarm off the tailnet is reported once via tailnet_self, not as gpu-ml down."""
        from tools import tailnet_self

        self.off.return_value = "NeedsLogin"
        with patch.object(chk, "fetch_log_tail", side_effect=ConnectionError("down")), \
             patch.object(tailnet_self, "report_off_tailnet") as rep, \
             patch.object(chk, "mail_archivist") as mail:
            self.assertEqual(chk.main([]), 0)
            rep.assert_called_once()
            self.assertEqual(rep.call_args[0][:2], ("supernote-selector-check", "NeedsLogin"))
            mail.assert_not_called()

    def test_reachable_run_notes_tailnet_reachable(self) -> None:
        log = "[2026-10-06T05:17:03Z] diff: admitted=1 stale_no_png=0"
        with patch.object(chk, "fetch_log_tail", return_value=log):
            chk.main(["--no-mail"])
        self.reach.assert_called_once()

    def test_flag_mails_archivist_unless_suppressed(self) -> None:
        log = "[2026-10-06T05:17:03Z] FLAG stale-set page(s) with NO P2 png: 1 (x)"
        with patch.object(chk, "fetch_log_tail", return_value=log), \
             patch.object(chk, "datetime") as dt, \
             patch.object(chk, "mail_archivist") as mail:
            dt.now.return_value = NOW
            dt.strptime = datetime.strptime
            self.assertEqual(chk.main([]), 1)
            mail.assert_called_once_with(1, 1, mail.call_args[0][2])
            mail.reset_mock()
            self.assertEqual(chk.main(["--no-mail"]), 1)
            mail.assert_not_called()

    def test_no_run_and_unreachable_also_mail_archivist(self) -> None:
        """T-4878: archivist owns the pipeline's alerts, so codes 2 and 3 mail too, not only a FLAG."""
        with patch.object(chk, "fetch_log_tail", side_effect=ConnectionError("down")), \
             patch.object(chk, "mail_archivist") as mail:
            self.assertEqual(chk.main([]), 3)
            self.assertEqual(mail.call_args[0][1], 3)
            self.assertIn("down", mail.call_args[0][2])
        with patch.object(chk, "fetch_log_tail", return_value=""), \
             patch.object(chk, "datetime") as dt, \
             patch.object(chk, "mail_archivist") as mail:
            dt.now.return_value = NOW
            dt.strptime = datetime.strptime
            self.assertEqual(chk.main([]), 2)
            self.assertEqual(mail.call_args[0][1], 2)
            mail.reset_mock()
            self.assertEqual(chk.main(["--no-mail"]), 2)
            mail.assert_not_called()

    def test_failure_mail_payloads_name_the_code(self) -> None:
        sent = []
        fake = type("M", (), {"send": staticmethod(lambda *a, **k: sent.append((a, k)))})
        with patch.dict("sys.modules", {"tools.mailbox": fake}), \
             patch("tools.agent_role_map.mailbox_hub", return_value="/nonexistent-hub"):
            import tools
            with patch.object(tools, "mailbox", fake, create=True):
                chk.mail_archivist(0, 2, "no ingest run")
                chk.mail_archivist(0, 3, "cannot read")
                chk.mail_archivist(4, 1, "")
        subjects = [k["subject"] for _, k in sent]
        self.assertIn("code 2", subjects[0])
        self.assertIn("code 3", subjects[1])
        self.assertIn("4 stale-set", subjects[2])


if __name__ == "__main__":
    unittest.main()
