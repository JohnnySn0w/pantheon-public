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
    def __init__(self, *, reachable=True, journal="", commit=NOW - HOUR, now=NOW):
        self._reachable, self._journal, self._commit, self._now = reachable, journal, commit, now
        self.alerts = []

    def host_reachable(self):
        return self._reachable

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

    def __init__(self, *, sha=TIP, runs=(), commit=NOW - 1800, now=NOW):
        self._sha, self._runs, self._commit, self._now = sha, runs, commit, now
        self.alerts = []

    def origin_tip_sha(self):
        return self._sha

    def audit_runs_for_sha(self, sha):
        return None if self._runs is None else list(self._runs)

    def origin_last_commit_epoch(self):
        return self._commit

    def alert(self, msg, vocab, agent=cal.AGENT):
        self.alerts.append((msg, vocab, agent))

    def now(self):
        return self._now


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

    # -- terminal non-success ------------------------------------------------
    def test_failed_run_is_AUDIT_FAILED_and_ALERTS(self):
        ops = FakeTipOps(runs=[run(conclusion="failure")])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)
        self.assertTrue(ops.alerts)

    def test_CANCELLED_counts_as_uncovered_not_as_covered(self):
        # Coverage-wise a cancelled audit leaves the tip exactly as unaudited as a red one.
        ops = FakeTipOps(runs=[run(conclusion="cancelled")])
        self.assertEqual(self._run(ops)["verdict"], cal.AUDIT_FAILED)

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

    def test_alerts_ride_thoths_lane_not_wards(self):
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


if __name__ == "__main__":
    unittest.main()
