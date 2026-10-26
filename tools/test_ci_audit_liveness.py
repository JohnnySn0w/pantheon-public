"""test_ci_audit_liveness.py — the origin-side audit's liveness detector (T-2440).

The properties under test are the ones that made the 2026-07-30 outage invisible for two days:
an unmeasurable state must not read as healthy, a quiet period with no commits must not page,
host-down must be its own verdict, and the mute must expire.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_audit_liveness as cal  # noqa: E402

HOUR = 3600
DAY = 24 * HOUR
NOW = 1785700000


def journal_with_success(ts: str) -> str:
    return (
        "2026-08-03 02:24:19Z: Running job: Scan added lines for byte-sequence violations\n"
        f"{ts}Z: Job Scan added lines for byte-sequence violations completed with result: Succeeded\n"
        "2026-08-03 02:24:39Z: Running job: tools suite (py3.10)\n"
    )


class FakeOps(cal.Ops):
    def __init__(self, *, reachable=True, journal="", commit=NOW - HOUR, now=NOW, off_tailnet=None):
        self._reachable, self._journal, self._commit, self._now = reachable, journal, commit, now
        self._off = off_tailnet
        self.alerts = []
        self.off_reports = []

    def host_reachable(self):
        return self._reachable

    def off_tailnet(self):  # T-5385: never read the real box's tailscale in tests
        return self._off

    def report_off_tailnet(self, state, detail):
        self.off_reports.append((state, detail))

    def journal_text(self):
        return self._journal

    def origin_last_commit_epoch(self):
        return self._commit

    def alert(self, msg, vocab, agent=cal.AGENT):
        self.alerts.append((msg, vocab, agent))

    def now(self):
        return self._now


class ParseTest(unittest.TestCase):
    def test_reads_the_runners_own_utc_stamp(self):
        # The Z in the runner's message is load-bearing: journald's own field is rendered in the
        # CONTAINER's timezone, which is not the host's. This must parse as UTC regardless of
        # whatever TZ the process running the test happens to be in.
        from datetime import datetime, timezone
        expected = int(datetime(2026, 8, 3, 2, 24, 23, tzinfo=timezone.utc).timestamp())
        got = cal.parse_success_epoch(journal_with_success("2026-08-03 02:24:23"))
        self.assertEqual(got, expected)

    def test_no_success_lines_is_none(self):
        self.assertIsNone(cal.parse_success_epoch(
            "2026-08-03 02:24:19Z: Running job: Scan added lines for byte-sequence violations\n"))

    def test_a_failed_job_is_not_a_success(self):
        self.assertIsNone(cal.parse_success_epoch(
            "2026-08-03 02:24:23Z: Job Scan added lines for byte-sequence violations "
            "completed with result: Failed\n"))

    def test_takes_the_NEWEST_success_not_the_last_line(self):
        text = (
            "2026-08-03 05:00:00Z: Job Scan added lines for byte-sequence violations "
            "completed with result: Succeeded\n"
            "2026-08-03 01:00:00Z: Job Scan added lines for byte-sequence violations "
            "completed with result: Succeeded\n"
        )
        newest = cal.parse_success_epoch(text)
        self.assertEqual(newest % 86400, 5 * HOUR, "must be the 05:00 entry, not the trailing one")


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, ops, **kw):
        return cal.check_once(ops=ops, state_dir=self.state, **kw)

    def _journal_at(self, epoch):
        from datetime import datetime, timezone
        return journal_with_success(
            datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    # -- the two-day outage, which is the whole point -----------------------
    def test_commits_landed_and_the_audit_never_ran_is_STALE(self):
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.STALE)
        self.assertTrue(ops.alerts, "a guard that stopped running must page")

    def test_audit_older_than_the_commits_by_more_than_threshold_is_STALE(self):
        ops = FakeOps(journal=self._journal_at(NOW - 2 * DAY), commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.STALE)

    # -- the false-positive this must not have ------------------------------
    def test_quiet_repo_with_no_commits_is_OK(self):
        # No audit in the window AND no recent commits: nothing was due. A weekend is not a fault.
        ops = FakeOps(journal="no audit here", commit=NOW - 5 * DAY)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.OK)
        self.assertEqual(ops.alerts, [])

    def test_audit_newer_than_the_last_commit_is_OK(self):
        ops = FakeOps(journal=self._journal_at(NOW - 60), commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.OK)
        self.assertEqual(ops.alerts, [])

    def test_a_draining_backlog_inside_the_threshold_is_OK(self):
        # Commit 2h after the last success, threshold 12h — the runner is behind, not dead.
        ops = FakeOps(journal=self._journal_at(NOW - 3 * HOUR), commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.OK)
        self.assertEqual(ops.alerts, [])

    # -- host-down is its OWN incident --------------------------------------
    def test_unreachable_host_is_HOST_DOWN_not_STALE(self):
        ops = FakeOps(reachable=False)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.HOST_DOWN)
        self.assertIn("HOST", ops.alerts[0][0])
        self.assertNotEqual(r["verdict"], cal.STALE,
                            "collapsing these describes a network outage as a stale audit")

    # -- UNKNOWN must never render as green ---------------------------------
    def test_unreadable_journal_is_UNKNOWN_and_alerts(self):
        ops = FakeOps(journal=None)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.UNKNOWN)
        self.assertTrue(ops.alerts, "unmeasurable must not pass silently")

    def test_undeterminable_origin_tip_is_UNKNOWN_and_alerts(self):
        ops = FakeOps(journal=self._journal_at(NOW - 60), commit=None)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.UNKNOWN)
        self.assertTrue(ops.alerts)

    # -- the authorised no ---------------------------------------------------
    def test_live_mute_suppresses_even_a_stale_guard(self):
        (self.state / cal._DISABLE_NAME).write_text(json.dumps(
            {"reason": "fleet pause", "until": "2100-01-01T00:00:00Z"}))
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.DISABLED)
        self.assertEqual(ops.alerts, [])

    def test_EXPIRED_mute_does_not_suppress(self):
        (self.state / cal._DISABLE_NAME).write_text(json.dumps(
            {"reason": "fleet pause", "until": "2000-01-01T00:00:00Z"}))
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.STALE,
                         "a mute that outlives its reason is how a monitor dies quietly")
        self.assertTrue(ops.alerts)

    def test_malformed_mute_does_not_suppress(self):
        (self.state / cal._DISABLE_NAME).write_text("{not json")
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        self.assertEqual(self._run(ops)["verdict"], cal.STALE)

    def test_mute_without_an_expiry_does_not_suppress(self):
        (self.state / cal._DISABLE_NAME).write_text(json.dumps({"reason": "forever pls"}))
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        self.assertEqual(self._run(ops)["verdict"], cal.STALE,
                         "an unbounded mute is not an authorised no, it is an abandoned one")

    # -- anti-spam, and its deliberate exception -----------------------------
    def test_same_verdict_is_throttled_by_the_cooldown(self):
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        self._run(ops)
        ops2 = FakeOps(journal="no audit here", commit=NOW - HOUR, now=NOW + 60)
        r2 = self._run(ops2)
        self.assertEqual(r2["verdict"], cal.STALE)
        self.assertEqual(ops2.alerts, [], "a repeat of the same fault must not re-page")

    def test_a_CHANGED_verdict_speaks_through_the_cooldown(self):
        ops = FakeOps(reachable=False)
        self._run(ops)
        ops2 = FakeOps(journal="no audit here", commit=NOW - HOUR, now=NOW + 60)
        r2 = self._run(ops2)
        self.assertEqual(r2["verdict"], cal.STALE)
        self.assertTrue(ops2.alerts,
                        "HOST_DOWN -> STALE is a different fault; throttling it hides the more "
                        "actionable one")

    def test_dry_run_never_alerts_and_never_writes_alert_state(self):
        ops = FakeOps(journal="no audit here", commit=NOW - HOUR)
        r = self._run(ops, dry_run=True)
        self.assertEqual(r["verdict"], cal.STALE)
        self.assertEqual(ops.alerts, [])
        state = json.loads((self.state / cal._STATE_NAME).read_text())
        self.assertNotIn("last_alert_epoch", state)


TIP = "6c44b7b96391df26f2a98e30755c73cce468ce6e"


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(*, status="completed", conclusion="success", created=NOW - 600):
    return {"id": 1, "status": status, "conclusion": conclusion,
            "created_at": _iso(created), "updated_at": _iso(created + 60),
            "html_url": "https://example.invalid/run/1"}


class FakeTipOps(cal.Ops):
    """Only the two GitHub-side probes differ from FakeOps; the rest is unused on this path."""

    _SAME = object()

    def __init__(self, *, sha=TIP, runs=(), commit=NOW - 1800, now=NOW, newer=(), requery=_SAME,
                 watermark=None):
        self._sha, self._runs, self._commit, self._now = sha, runs, commit, now
        # T-5001: the scheduled-audit watermark answer. None = unmeasurable, which leaves the
        # per-sha verdict untouched (so every pre-T-5001 test keeps its meaning).
        self._watermark = watermark
        self.watermark_queries = []
        self._newer = newer
        # T-4652: what the SECOND per-sha query returns (default: the same as the first).
        self._requery = runs if requery is FakeTipOps._SAME else requery
        self.sha_queries = 0
        self.sleeps = []
        self.newer_queries = []
        self.alerts = []

    def origin_tip_sha(self):
        return self._sha

    def audit_runs_for_sha(self, sha):
        self.sha_queries += 1
        answer = self._runs if self.sha_queries == 1 else self._requery
        return None if answer is None else list(answer)

    # The real Ops.sleep would make every NOT_DISPATCHED test wait out the re-query delay.
    def sleep(self, secs):
        self.sleeps.append(secs)

    # T-4199 N2. Overridden here because FakeTipOps subclasses the REAL Ops, whose version shells
    # out to `gh`: forgetting this seam would make every cancelled-tip test hit GitHub.
    def audit_runs_created_after(self, since_epoch, *, exclude_sha):
        self.newer_queries.append((since_epoch, exclude_sha))
        return None if self._newer is None else list(self._newer)

    def origin_last_commit_epoch(self):
        return self._commit

    # T-5001: overridden for the same reason as audit_runs_created_after (the real one calls gh).
    def watermark_coverage(self, tip_sha):
        self.watermark_queries.append(tip_sha)
        return None if self._watermark is None else dict(self._watermark)

    def alert(self, msg, vocab, agent=cal.AGENT):
        self.alerts.append((msg, vocab, agent))

    def now(self):
        return self._now


class ScheduledWatermarkTest(unittest.TestCase):
    """T-5001: main is audited on a */15 schedule, so the tip normally has no run of its own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def _run(self, ops, **kw):
        return cal.check_tip_once(ops=ops, state_dir=self.state, requery_delay_secs=0, **kw)

    def test_a_scheduled_run_whose_head_contains_the_tip_is_COVERED(self):
        sched = run()
        sched["event"] = "schedule"
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR,
                         watermark={"covered_by": sched, "oldest_unaudited_epoch": None})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.COVERED)
        self.assertEqual(ops.alerts, [])
        self.assertEqual(ops.watermark_queries, [TIP])

    def test_unaudited_commits_inside_the_grace_are_PENDING_and_SILENT(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR,
                         watermark={"covered_by": None, "oldest_unaudited_epoch": NOW - 20 * 60})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.PENDING)
        self.assertEqual(ops.alerts, [], "a commit waiting for the next */15 run is normal")

    def test_unaudited_commits_past_the_grace_ALERT(self):
        ops = FakeTipOps(runs=[], commit=NOW - 60,
                         watermark={"covered_by": None, "oldest_unaudited_epoch": NOW - 2 * HOUR})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertTrue(ops.alerts, "the schedule stopped covering main: that is the fault")
        self.assertIn("schedule", r["detail"])

    def test_the_grace_is_well_above_the_schedule_cadence(self):
        self.assertGreaterEqual(cal.DEFAULT_SCHEDULE_GRACE_SECS, 45 * 60)

    def test_no_watermark_at_all_ALERTS(self):
        ops = FakeTipOps(runs=[], commit=NOW - 60,
                         watermark={"covered_by": None, "oldest_unaudited_epoch": None})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertTrue(ops.alerts)

    def test_watermark_is_not_consulted_when_the_tip_has_its_own_run(self):
        ops = FakeTipOps(runs=[run()], watermark={"covered_by": None,
                                                 "oldest_unaudited_epoch": NOW - 9 * HOUR})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.COVERED)
        self.assertEqual(ops.watermark_queries, [])

    def test_a_red_tip_run_is_never_masked_by_the_watermark(self):
        ops = FakeTipOps(runs=[run(conclusion="failure")],
                         watermark={"covered_by": run(), "oldest_unaudited_epoch": None})
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.AUDIT_FAILED)


class TipCoverageTest(unittest.TestCase):
    """Tip coverage answers a DIFFERENT question from liveness, and the tests that matter are the
    ones separating them: a queued tip and an undispatched tip both look like 'no success yet'."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, ops, **kw):
        return cal.check_tip_once(ops=ops, state_dir=self.state, **kw)

    # -- the claim the retire-condition actually needs -----------------------
    def test_concluded_success_for_the_tip_sha_is_COVERED(self):
        ops = FakeTipOps(runs=[run()])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.COVERED)
        self.assertEqual(r["tip_sha"], TIP)
        self.assertEqual(ops.alerts, [])

    # -- the 2026-08-03 state: liveness said OK, the tip was NOT audited -----
    def test_queued_past_the_threshold_is_PENDING_and_ALERTS(self):
        ops = FakeTipOps(runs=[run(status="queued", conclusion=None, created=NOW - 10 * HOUR)])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.PENDING)
        self.assertTrue(ops.alerts, "a tip sitting hours behind a backlog is the whole point")

    def test_freshly_queued_is_PENDING_and_SILENT(self):
        ops = FakeTipOps(runs=[run(status="queued", conclusion=None, created=NOW - 120)])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.PENDING)
        self.assertEqual(ops.alerts, [], "ordinary latency must not page every hour")

    # -- the split the ticket asked for: not-yet has TWO causes -------------
    def test_no_run_at_all_past_the_grace_is_NOT_DISPATCHED_not_PENDING(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertNotEqual(r["verdict"], cal.PENDING,
                            "a run GitHub never created is not a run waiting on the runner, and "
                            "collapsing them sends the reader to the wrong host")
        self.assertTrue(ops.alerts)

    def test_no_run_yet_inside_the_dispatch_grace_is_SILENT(self):
        ops = FakeTipOps(runs=[], commit=NOW - 60)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertEqual(ops.alerts, [], "a push seconds old has not had time to be dispatched")

    # -- T-4652: an empty per-sha answer is asked once more before it may page --------------------
    # 2026-10-05 12:38Z: GitHub's head_sha filter returned [] (rc 0) for a run that existed and had
    # concluded success; the same query 2m later found it.
    def test_empty_then_found_on_requery_is_COVERED_and_SILENT(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR, requery=[run()])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.COVERED)
        self.assertEqual(ops.alerts, [], "a transient empty answer must not page")
        self.assertEqual((ops.sha_queries, ops.sleeps), (2, [cal.DEFAULT_REQUERY_DELAY_SECS]))
        self.assertIn("re-query", r["detail"])

    def test_empty_twice_is_NOT_DISPATCHED_and_ALERTS_saying_it_was_confirmed(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertTrue(ops.alerts)
        self.assertEqual(ops.sha_queries, 2)
        self.assertIn("Confirmed by a re-query", r["detail"])

    def test_a_failed_requery_keeps_the_NOT_DISPATCHED_alert(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR, requery=None)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertTrue(ops.alerts, "fail toward speaking: the first answer was a real read")
        self.assertIn("FAILED", r["detail"])

    def test_requery_that_finds_a_red_run_reports_the_red(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR, requery=[run(conclusion="failure")])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.AUDIT_FAILED)
        self.assertTrue(ops.alerts)

    def test_no_requery_when_the_empty_answer_would_not_alert_anyway(self):
        ops = FakeTipOps(runs=[], commit=NOW - 60, requery=[run()])   # inside the grace
        self._run(ops)
        self.assertEqual((ops.sha_queries, ops.sleeps), (1, []))

    def test_no_requery_when_the_first_answer_has_runs(self):
        ops = FakeTipOps(runs=[run(conclusion="failure")], requery=[run()])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)
        self.assertEqual((ops.sha_queries, ops.sleeps), (1, []))

    def test_requery_delay_zero_disables_it(self):
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR, requery=[run()])
        r = self._run(ops, requery_delay_secs=0)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertEqual((ops.sha_queries, ops.sleeps), (1, []))

    # -- terminal non-success ------------------------------------------------
    def test_failed_run_is_AUDIT_FAILED_and_ALERTS(self):
        ops = FakeTipOps(runs=[run(conclusion="failure")])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)
        self.assertTrue(ops.alerts)

    def test_CANCELLED_counts_as_uncovered_not_as_covered(self):
        # Coverage-wise a cancelled audit leaves the tip exactly as unaudited as a red one.
        ops = FakeTipOps(runs=[run(conclusion="cancelled")])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)

    # -- T-4199 N2: supersession makes a cancelled tip run NORMAL, not a fault ------------------
    # hermes-audit has a concurrency group (T-4182): a push landing between the monitor's fetch and
    # its per-sha query cancels the tip's PENDING run in favour of the newer one, and the newer
    # run's watermark range covers the cancelled commit. Read as AUDIT_FAILED that is a false alert
    # (2026-09-28: 76 of the last 100 main-push audit runs were cancelled).
    def test_cancelled_tip_with_a_newer_run_on_the_ref_is_superseded_PENDING_and_SILENT(self):
        newer = {"id": 2, "status": "queued", "conclusion": None, "head_sha": "d" * 40,
                 "created_at": _iso(NOW - 30), "html_url": "https://example.invalid/run/2"}
        ops = FakeTipOps(runs=[run(conclusion="cancelled", created=NOW - 600)], newer=[newer])
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.PENDING)
        self.assertEqual(ops.alerts, [], "a superseded run must not page")
        self.assertIn("supersed", r["detail"])
        self.assertIn("https://example.invalid/run/2", r["detail"])

    def test_the_newer_run_query_is_after_the_cancelled_run_and_excludes_the_tip(self):
        ops = FakeTipOps(runs=[run(conclusion="cancelled", created=NOW - 900),
                               run(conclusion="cancelled", created=NOW - 600)],
                         newer=[{"id": 2, "head_sha": "d" * 40, "created_at": _iso(NOW - 30)}])
        self._run(ops)
        self.assertEqual(ops.newer_queries, [(NOW - 600, TIP)])  # newest cancelled run's start

    def test_cancelled_tip_with_NO_newer_run_still_ALERTS(self):
        # A manual cancel of the newest run leaves the tip genuinely unaudited: nothing covers it.
        ops = FakeTipOps(runs=[run(conclusion="cancelled")], newer=[])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)
        self.assertTrue(ops.alerts)

    def test_a_failed_newer_run_query_keeps_the_alert(self):
        # Fail toward speaking: if we cannot prove a newer run exists, the tip is not known covered.
        ops = FakeTipOps(runs=[run(conclusion="cancelled")], newer=None)
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)
        self.assertTrue(ops.alerts)

    def test_only_cancelled_is_ever_downgraded_a_red_run_is_never_masked_by_a_newer_one(self):
        newer = [{"id": 2, "head_sha": "d" * 40, "created_at": _iso(NOW - 30)}]
        for conclusion in ("failure", "timed_out", "startup_failure", "action_required"):
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as fresh:
                # a fresh state dir per case: the alert cooldown would otherwise mute the repeats
                ops = FakeTipOps(runs=[run(conclusion=conclusion)], newer=newer)
                r = cal.check_tip_once(ops=ops, state_dir=Path(fresh))
                self.assertEqual(r["verdict"], cal.AUDIT_FAILED)
                self.assertEqual(ops.newer_queries, [], "no reason to ask")
                self.assertTrue(ops.alerts)

    def test_cancelled_alongside_a_real_failure_stays_red_even_with_a_newer_run(self):
        newer = [{"id": 2, "head_sha": "d" * 40, "created_at": _iso(NOW - 30)}]
        ops = FakeTipOps(runs=[run(conclusion="cancelled"), run(conclusion="failure")], newer=newer)
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)

    def test_the_pure_classifier_refuses_to_downgrade_a_red_run_even_if_handed_newer_runs(self):
        # Defence in depth: the caller does not ask for non-cancelled tips, but the decision itself
        # must not trust that. `newer_runs` may only ever explain a CANCELLED run away.
        newer = [{"id": 2, "head_sha": "d" * 40}]
        for conclusion in ("failure", "timed_out"):
            with self.subTest(conclusion=conclusion):
                d = cal.classify_tip_coverage([run(conclusion=conclusion), run(conclusion="cancelled")],
                                              now=NOW, tip_sha=TIP, commit_epoch=NOW - 900,
                                              newer_runs=newer)
                self.assertEqual((d["verdict"], d["should_alert"]), (cal.AUDIT_FAILED, True))

    def test_no_newer_run_query_when_the_tip_is_covered_or_has_no_cancelled_run(self):
        for runs in ([run(conclusion="cancelled"), run(conclusion="success")],
                     [run(status="queued", conclusion=None)], []):
            with self.subTest(runs=len(runs)):
                ops = FakeTipOps(runs=runs)
                self._run(ops)
                self.assertEqual(ops.newer_queries, [])

    def test_a_success_anywhere_in_the_set_beats_an_earlier_failure(self):
        # Re-run green after a red: the tip IS audited. Order in the response must not decide it.
        ops = FakeTipOps(runs=[run(conclusion="failure"), run(conclusion="success")])
        self.assertEqual(self._run(ops)["verdict"], cal.COVERED)

    def test_a_terminal_red_is_reported_even_while_a_rerun_is_queued(self):
        ops = FakeTipOps(runs=[run(conclusion="failure"),
                               run(status="queued", conclusion=None, created=NOW - 60)])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED,
                         "biasing toward speaking: the red is actionable now and the re-run "
                         "does not undo it")

    # -- unmeasurable must never render as green ----------------------------
    def test_failed_github_query_is_UNKNOWN_and_ALERTS(self):
        ops = FakeTipOps(runs=None)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.UNKNOWN)
        self.assertTrue(ops.alerts, "gh unauthenticated or the workflow renamed is not health")

    def test_undeterminable_tip_sha_is_UNKNOWN_and_ALERTS(self):
        ops = FakeTipOps(sha=None)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.UNKNOWN)
        self.assertTrue(ops.alerts)

    # -- the two signals are independently muted ----------------------------
    def test_the_LIVENESS_mute_does_not_silence_tip_coverage(self):
        # A planned runner outage is a reason to silence execution liveness. "The tip is not
        # audited" stays true throughout it, and must keep being said.
        (self.state / cal._DISABLE_NAME).write_text(json.dumps(
            {"reason": "planned pve2 maintenance", "until": "2100-01-01T00:00:00Z"}))
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR)
        r = self._run(ops)
        self.assertEqual(r["verdict"], cal.NOT_DISPATCHED)
        self.assertTrue(ops.alerts)

    def test_its_OWN_mute_does_silence_it_and_still_expires(self):
        (self.state / cal._TIP_DISABLE_NAME).write_text(json.dumps(
            {"reason": "known backlog", "until": "2100-01-01T00:00:00Z"}))
        ops = FakeTipOps(runs=[], commit=NOW - 6 * HOUR)
        self.assertEqual(self._run(ops)["verdict"], cal.DISABLED)
        self.assertEqual(ops.alerts, [])

        (self.state / cal._TIP_DISABLE_NAME).write_text(json.dumps(
            {"reason": "known backlog", "until": "2000-01-01T00:00:00Z"}))
        ops2 = FakeTipOps(runs=[], commit=NOW - 6 * HOUR)
        self.assertEqual(self._run(ops2)["verdict"], cal.NOT_DISPATCHED)

    # -- state separation ----------------------------------------------------
    def test_tip_writes_its_own_state_file_not_the_liveness_one(self):
        ops = FakeTipOps(runs=[run()])
        self._run(ops)
        self.assertTrue((self.state / cal._TIP_STATE_NAME).exists())
        self.assertFalse((self.state / cal._STATE_NAME).exists(),
                         "shared state would cross-throttle two independent instruments")

    def test_alerts_ride_orchestrators_lane_not_recoveries(self):
        ops = FakeTipOps(runs=None)
        self._run(ops)
        self.assertEqual(ops.alerts[0][2], cal.TIP_AGENT)

    def test_dry_run_never_alerts(self):
        ops = FakeTipOps(runs=None)
        r = self._run(ops, dry_run=True)
        self.assertEqual(r["verdict"], cal.UNKNOWN)
        self.assertEqual(ops.alerts, [])


class TipQueryShapeTest(unittest.TestCase):
    """The windowing trap is a property of HOW we ask, so it is worth a test of its own."""

    def test_the_query_filters_by_head_sha_and_never_lists_a_window(self):
        seen = {}
        import subprocess as sp
        real = sp.run

        def fake_run(argv, *a, **kw):
            seen["argv"] = argv
            class R:
                returncode = 0
                stdout = json.dumps({"total_count": 0, "workflow_runs": []})
            return R()

        sp.run = fake_run
        try:
            cal.Ops().audit_runs_for_sha(TIP)
        finally:
            sp.run = real

        argv = seen["argv"]
        self.assertEqual(argv[:2], ["gh", "api"])
        self.assertIn(f"head_sha={TIP}", argv[2])
        self.assertNotIn("--limit", argv,
                         "a bounded listing is the exact instrument that reported a frozen queue "
                         "while the runner finished a job every six seconds")


class NewerRunQueryShapeTest(unittest.TestCase):
    """`audit_runs_created_after` is an EXISTENCE check, so it uses GitHub's server-side `created=`
    filter: a bounded listing that merely happened not to include a newer run could read as
    "no newer run" and page. Any non-empty answer is decisive; truncation cannot produce a false
    negative."""

    def _ask(self, stdout, returncode=0):
        seen = {}
        import subprocess as sp
        real = sp.run

        def fake_run(argv, *a, **kw):
            seen["argv"] = argv

            class R:
                pass

            r = R()
            r.returncode, r.stdout = returncode, stdout
            return r

        sp.run = fake_run
        try:
            out = cal.Ops().audit_runs_created_after(NOW - 600, exclude_sha=TIP)
        finally:
            sp.run = real
        return out, seen["argv"]

    def test_the_query_is_main_push_runs_created_at_or_after_the_cancelled_run(self):
        _, argv = self._ask(json.dumps({"workflow_runs": []}))
        self.assertEqual(argv[:2], ["gh", "api"])
        endpoint = argv[2]
        self.assertIn(f"/actions/workflows/{cal.AUDIT_WORKFLOW_FILE}/runs", endpoint)
        self.assertIn("branch=main", endpoint)
        self.assertIn("event=push", endpoint)
        self.assertIn("created=%3E%3D" + _iso(NOW - 600).replace(":", "%3A"), endpoint)
        self.assertNotIn("--limit", argv)

    def test_the_tip_own_runs_are_excluded_so_only_a_different_commit_counts(self):
        body = json.dumps({"workflow_runs": [
            {"id": 1, "head_sha": TIP, "created_at": _iso(NOW - 500)},
            {"id": 2, "head_sha": "d" * 40, "created_at": _iso(NOW - 100)},
        ]})
        out, _ = self._ask(body)
        self.assertEqual([r["id"] for r in out], [2])

    def test_only_the_tips_own_runs_means_no_newer_run(self):
        body = json.dumps({"workflow_runs": [{"id": 1, "head_sha": TIP}]})
        out, _ = self._ask(body)
        self.assertEqual(out, [])  # an empty list is a real answer, not a failure

    def test_a_failed_call_or_bad_body_is_none_never_an_empty_list(self):
        well_formed = json.dumps({"workflow_runs": [{"id": 9, "head_sha": "d" * 40}]})
        for stdout, rc in (("", 1), ("not json", 0), (json.dumps({"nope": []}), 0),
                           (well_formed, 1)):  # a non-zero gh exit is a failure whatever it printed
            with self.subTest(rc=rc, stdout=stdout[:8]):
                self.assertIsNone(self._ask(stdout, rc)[0])


class JournalQueryShapeTest(unittest.TestCase):
    """T-4355: the audit moved to the LIGHT runner. Reading only the suite runner's journal would
    find no successes there and report the audit STALE while it runs fine next door."""

    def test_the_journal_query_reads_both_runner_units(self):
        seen = {}
        import subprocess as sp
        real = sp.run

        def fake_run(argv, *a, **kw):
            seen["argv"] = argv
            class R:
                returncode = 0
                stdout = ""
            return R()

        sp.run = fake_run
        try:
            cal.Ops().journal_text()
        finally:
            sp.run = real

        remote = seen["argv"][-1]
        for unit in ("actions.runner.example-org-pantheon-config.pve2-pantheon-config.service",
                     "actions.runner.example-org-pantheon-config.pve2-pantheon-config-light.service"):
            with self.subTest(unit):
                self.assertIn(f"-u {unit} ", remote + " ")


class OffTailnetT5385Test(unittest.TestCase):
    def test_host_down_while_this_host_is_off_the_tailnet_is_not_a_host_alert(self):
        with tempfile.TemporaryDirectory() as d:
            ops = FakeOps(reachable=False, off_tailnet="NeedsLogin")
            r = cal.check_once(ops=ops, state_dir=Path(d))
        self.assertEqual(r["verdict"], cal.OFF_TAILNET)
        self.assertEqual(ops.alerts, [])
        self.assertEqual(ops.off_reports[0][0], "NeedsLogin")

    def test_host_down_on_the_tailnet_still_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            ops = FakeOps(reachable=False)
            r = cal.check_once(ops=ops, state_dir=Path(d))
        self.assertEqual(r["verdict"], cal.HOST_DOWN)
        self.assertEqual(len(ops.alerts), 1)


if __name__ == "__main__":
    unittest.main()
