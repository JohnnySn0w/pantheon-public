"""T-4402: the nightly-CI red-run watcher.

The properties under test are the ones that let the nightly sit red for 20 days: a red run
must reach orchestrator ONCE, a missing run must alert, an unmeasurable state must alert rather
than read as green, an empty read must never read as "no run", and a failed delivery must
be loud and retried rather than silently marked as sent.

No network: every GitHub / mailbox touch goes through the `Ops` seam, which the tests
replace. The log-extraction fixtures are VERBATIM lines from the real 2026-09-27 nightly
(run 36326511096, job "tools suite (py3.11)") and the real output of the two outcome
checkers run on a crafted report (D11: fixtures come from the real surface, not from
assumption).
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from tools import ci_nightly_watch as nw

HOUR = 3600
NOW = int(datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc).timestamp())

# --- verbatim from the real run's job log (timestamps included) ---------------------------
REAL_PYTEST_LOG = """\
2026-09-27T14:38:58.6091840Z tools/test_watch_runner.py:3191: AssertionError
2026-09-27T14:38:58.6092172Z =========================== short test summary info ============================
2026-09-27T14:38:58.6093110Z FAILED tools/test_pgrep_predicate_lint.py::TestLiveTree::test_live_tree_is_clean - FileNotFoundError: [Errno 2] No such file or directory: '/opt/actions-runner-pc/_work/pantheon-config/pantheon-config/tmpzmqpxby6'
2026-09-27T14:38:58.6094291Z FAILED tools/test_ticket_purpose.py::TicketPurposeTests::test_initial_backfill_never_infers_project_from_repo_or_persona - AssertionError: Lists differ: ['T-3986'] != []
2026-09-27T14:38:58.6094850Z
2026-09-27T14:38:58.6094961Z First list contains 1 additional elements.
2026-09-27T14:38:58.6095260Z First extra element 0:
2026-09-27T14:38:58.6095458Z 'T-3986'
2026-09-27T14:38:58.6095564Z
2026-09-27T14:38:58.6096006Z - ['T-3986']
2026-09-27T14:38:58.6096221Z + []
2026-09-27T14:38:58.6097636Z FAILED tools/test_watch_runner.py::TestTopicPolicy::test_every_active_topic_has_policy_entry - AssertionError: ['anthropic-cyber-verification-models'] is not false : ACTIVE topics missing TOPIC_POLICY entry in tools/watch_runner.py: ['anthropic-cyber-verification-models']. Each such topic falls through to _DEFAULT_POLICY and its findings are silently undelivered. Add a TOPIC_POLICY entry (target / labels / interactive_required) for each.
2026-09-27T14:38:58.6099640Z FAILED tools/test_watch_runner.py::UnregisteredTopicPolicyTest::test_every_active_topic_in_the_real_topics_file_is_registered - AssertionError: Lists differ: ['anthropic-cyber-verification-models'] != []
2026-09-27T14:38:58.6100326Z
2026-09-27T14:38:58.6100427Z First list contains 1 additional elements.
2026-09-27T14:38:58.6100699Z First extra element 0:
2026-09-27T14:38:58.6101037Z 'anthropic-cyber-verification-models'
2026-09-27T14:38:58.6101218Z
2026-09-27T14:38:58.6101336Z - ['anthropic-cyber-verification-models']
2026-09-27T14:38:58.6101745Z + [] : active topics missing a TOPIC_POLICY entry: ['anthropic-cyber-verification-models']
2026-09-27T14:38:58.6102246Z 4 failed, 10273 passed, 378 skipped, 2164 subtests passed in 156.13s (0:02:36)
2026-09-27T14:38:58.7066246Z ##[error]Process completed with exit code 1.
2026-09-27T14:38:58.7182957Z Post job cleanup.
"""

# --- the real checkers' failure output on a crafted report (GitHub adds the timestamp) -------
REAL_CHECKER_LOG = """\
2026-09-30T13:01:02.1000000Z check_shell_suite_outcomes: 17 problem(s), the shell suites did not all pass:
2026-09-30T13:01:02.1000001Z   no test_shell_suite_passes testcases in pytest-tools.xml at all: the wrapper did not run (deselected, ignored or not collected)
2026-09-30T13:01:02.1000002Z   test_confined_load.sh: no testcase test_shell_suite_passes[test_confined_load.sh] in the report (never ran: deselected, ignored or not collected)
2026-09-30T13:01:02.2000000Z ##[error]Process completed with exit code 1.
2026-09-30T13:01:03.0000000Z check_suite_outcome_floor: the Python suite's outcome falls short of its committed baseline:
2026-09-30T13:01:03.0000001Z   executed 1 tests, below the floor executed_min=12683 (12682 short)
2026-09-30T13:01:03.0000002Z   If fewer tests running is INTENDED (tests deleted or deliberately skipped), edit ci_outcome_baseline.json in THIS PR (executed_min / skipped_max, and `_provenance` with the new observed counts and why) so review sees the drop.
2026-09-30T13:01:03.1000000Z Post job cleanup.
"""


def iso(secs_ago: float) -> str:
    return datetime.fromtimestamp(NOW - secs_ago, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(rid: int, conclusion: str | None, ago_h: float, *, status: str = "completed",
        attempt: int = 1, sha: str = "ca90576e4b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e") -> dict:
    return {"id": rid, "run_attempt": attempt, "status": status, "conclusion": conclusion,
            "created_at": iso(ago_h * HOUR), "html_url": f"https://github.com/x/y/actions/runs/{rid}",
            "head_sha": sha, "event": "schedule"}


def job(jid: int, name: str, conclusion: str = "failure", steps=("Run tools test suite",)) -> dict:
    return {"id": jid, "name": name, "conclusion": conclusion,
            "steps": [{"name": s, "conclusion": "failure", "number": i + 1}
                      for i, s in enumerate(steps)]}


class FakeOps(nw.Ops):
    def __init__(self, runs=None, *, error=None, jobs=None, jobs_error=None, logs=None,
                 log_error=None, send_fails=0, now=NOW, runs_seq=None):
        self.runs, self.error = runs or [], error
        self.runs_seq, self.reads = list(runs_seq or []), 0
        self.jobs, self.jobs_error = jobs or [], jobs_error
        self.logs, self.log_error = logs or {}, log_error
        self.send_fails, self._now = send_fails, now
        self.sent: list[tuple[str, str]] = []
        self.sleeps: list[float] = []

    def scheduled_runs(self):
        self.reads += 1
        if self.runs_seq:
            item = self.runs_seq.pop(0) if len(self.runs_seq) > 1 else self.runs_seq[0]
            if isinstance(item, Exception):
                raise item
            return [dict(r) for r in item]
        if self.error:
            raise nw.GhError(self.error)
        return [dict(r) for r in self.runs]

    def run_jobs(self, run_id):
        if self.jobs_error:
            raise nw.GhError(self.jobs_error)
        return list(self.jobs)

    def job_log(self, job_id):
        if self.log_error:
            raise nw.GhError(self.log_error)
        return self.logs.get(job_id, "")

    def alert(self, subject, body):
        if self.send_fails > 0:
            self.send_fails -= 1
            raise OSError("mailbox down")
        self.sent.append((subject, body))

    def now(self):
        return self._now

    def sleep(self, secs):
        self.sleeps.append(secs)


class TmpStateCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.state = Path(self._td.name)

    def check(self, ops, **kw):
        kw.setdefault("dead_secs", 36 * HOUR)
        kw.setdefault("pending_secs", 8 * HOUR)
        kw.setdefault("cooldown_secs", 12 * HOUR)
        return nw.check_once(ops=ops, state_dir=self.state, **kw)

    def state_json(self) -> dict:
        return json.loads((self.state / "ci-nightly-watch.json").read_text())


class ExtractFailureLinesTests(unittest.TestCase):
    def test_real_pytest_log_yields_the_four_failed_nodeids_and_summary(self):
        got = nw.extract_failure_lines(REAL_PYTEST_LOG)
        failed = got["failed"]
        self.assertEqual(len(failed), 4)
        self.assertTrue(all(line.startswith("FAILED tools/test_") for line in failed))
        self.assertIn("tools/test_ticket_purpose.py::TicketPurposeTests::"
                      "test_initial_backfill_never_infers_project_from_repo_or_persona",
                      failed[1])
        self.assertIn("Lists differ: ['T-3986'] != []", failed[1])
        # pytest's continuation lines and the timestamps are not part of a FAILED line
        self.assertFalse(any("First list contains" in line for line in failed))
        self.assertFalse(any("2026-09-27T" in line for line in failed))
        self.assertEqual(got["summary"],
                         "4 failed, 10273 passed, 378 skipped, 2164 subtests passed in 156.13s (0:02:36)")
        self.assertEqual(got["errors"], ["Process completed with exit code 1."])

    def test_long_failed_lines_are_truncated(self):
        failed = nw.extract_failure_lines(REAL_PYTEST_LOG)["failed"]
        self.assertTrue(all(len(line) <= nw.MAX_LINE_CHARS for line in failed))
        self.assertTrue(failed[2].endswith("..."))  # the ~400-char TOPIC_POLICY line
        self.assertIn("test_every_active_topic_has_policy_entry", failed[2])

    def test_real_checker_blocks_are_captured_with_their_indented_detail(self):
        got = nw.extract_failure_lines(REAL_CHECKER_LOG)
        blocks = got["checker"]
        self.assertTrue(blocks[0].startswith("check_shell_suite_outcomes: 17 problem(s)"))
        self.assertTrue(any("test_confined_load.sh: no testcase" in b for b in blocks))
        self.assertTrue(any(b.startswith("check_suite_outcome_floor:") for b in blocks))
        self.assertTrue(any("executed 1 tests, below the floor executed_min=12683" in b
                            for b in blocks))
        # the block ends at the first non-indented line
        self.assertFalse(any("Post job cleanup" in b for b in blocks))
        self.assertFalse(any(b.startswith("##[error]") for b in blocks))

    def test_failed_lines_are_capped_with_a_count_of_the_rest(self):
        log = "".join(f"2026-09-27T14:38:58.0000000Z FAILED tools/test_a.py::T::t{i} - boom\n"
                      for i in range(nw.MAX_FAILED_LINES + 7))
        got = nw.extract_failure_lines(log)
        self.assertEqual(len(got["failed"]), nw.MAX_FAILED_LINES)
        self.assertEqual(got["failed_omitted"], 7)

    def test_collection_errors_count_but_unrelated_error_lines_do_not(self):
        log = ("2026-09-27T14:38:58Z ERROR tools/test_x.py - ImportError: no module named y\n"
               "2026-09-27T14:38:58Z ERROR: pip's dependency resolver does not currently take\n"
               "2026-09-27T14:38:58Z ERROR: Could not find a version\n")
        got = nw.extract_failure_lines(log)
        self.assertEqual(got["failed"], ["ERROR tools/test_x.py - ImportError: no module named y"])

    def test_empty_and_garbage_logs_extract_nothing(self):
        for text in ("", "not a log\n", "\n\n"):
            got = nw.extract_failure_lines(text)
            self.assertEqual((got["failed"], got["checker"], got["errors"], got["summary"]),
                             ([], [], [], None))


class RealOpsReadTests(unittest.TestCase):
    """The real Ops with only `gh_api` and `sleep` stubbed."""

    class Stubbed(nw.Ops):
        def __init__(self, replies):
            self.replies, self.endpoints, self.sleeps = list(replies), [], []

        def gh_api(self, endpoint):
            self.endpoints.append(endpoint)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def sleep(self, secs):
            self.sleeps.append(secs)

    FULL = json.dumps({"total_count": 1, "workflow_runs": [run(1, "success", 3)]})
    EMPTY = json.dumps({"total_count": 0, "workflow_runs": []})

    def test_reads_the_workflow_level_endpoint_never_the_repo_level_one(self):
        ops = self.Stubbed([self.FULL])
        ops.scheduled_runs()
        (endpoint,) = ops.endpoints
        self.assertIn(f"repos/{nw.GH_REPO}/actions/workflows/{nw.WORKFLOW_ID}/runs", endpoint)
        self.assertIn("event=schedule", endpoint)
        self.assertNotIn("/actions/runs?", endpoint)
        self.assertEqual(nw.WORKFLOW_ID, "304978944")

    def test_an_empty_read_is_retried_and_a_later_full_read_wins(self):
        ops = self.Stubbed([self.EMPTY, self.FULL])
        self.assertEqual([r["id"] for r in ops.scheduled_runs()], [1])
        self.assertEqual(len(ops.endpoints), 2)
        self.assertEqual(len(ops.sleeps), 1)

    def test_empty_on_every_attempt_is_an_error_never_an_empty_list(self):
        ops = self.Stubbed([self.EMPTY] * nw.EMPTY_READ_ATTEMPTS)
        with self.assertRaises(nw.GhError) as cm:
            ops.scheduled_runs()
        self.assertIn("empty", str(cm.exception))
        self.assertEqual(len(ops.endpoints), nw.EMPTY_READ_ATTEMPTS)

    def test_bad_json_and_missing_key_and_gh_failure_are_gh_errors(self):
        for reply in ("not json", json.dumps({"total_count": 3}), json.dumps([1, 2]),
                      nw.GhError("gh exited 1: not logged in")):
            with self.subTest(reply=str(reply)[:30]):
                with self.assertRaises(nw.GhError):
                    self.Stubbed([reply]).scheduled_runs()

    def test_run_jobs_and_job_log_use_the_expected_endpoints(self):
        ops = self.Stubbed([json.dumps({"jobs": [job(5, "tools suite (py3.12)")]}), "LOGTEXT"])
        self.assertEqual(ops.run_jobs(99)[0]["id"], 5)
        self.assertEqual(ops.job_log(5), "LOGTEXT")
        self.assertEqual(ops.endpoints, [
            f"repos/{nw.GH_REPO}/actions/runs/99/jobs?per_page=100",
            f"repos/{nw.GH_REPO}/actions/jobs/5/logs"])


class GhLayerTests(unittest.TestCase):
    """The real `gh` subprocess wrapper with `subprocess.run` stubbed."""

    def _completed(self, rc=0, out="{}", err=""):
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()

    def test_success_returns_stdout_and_calls_gh_api_with_the_endpoint(self):
        with patch("subprocess.run", return_value=self._completed(out='{"a": 1}')) as run_:
            self.assertEqual(nw.Ops().gh_api("repos/x/y"), '{"a": 1}')
        self.assertEqual(run_.call_args.args[0], ["gh", "api", "repos/x/y"])

    def test_nonzero_exit_timeout_and_missing_binary_are_gh_errors(self):
        import subprocess as sp
        cases = [
            patch("subprocess.run", return_value=self._completed(rc=1, err="not logged in")),
            patch("subprocess.run", side_effect=sp.TimeoutExpired("gh", 45)),
            patch("subprocess.run", side_effect=FileNotFoundError("gh")),
        ]
        for p in cases:
            with self.subTest(p=str(p.kwargs)[:40]), p:
                with self.assertRaises(nw.GhError):
                    nw.Ops().gh_api("repos/x/y")

    def test_a_substitute_ops_returning_an_empty_list_is_unknown_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            ops = FakeOps([])
            result = nw.check_once(ops=ops, state_dir=Path(td))
            self.assertEqual(result["verdict"], nw.UNKNOWN)
            self.assertEqual(len(ops.sent), 1)


class RealDeliveryTests(unittest.TestCase):
    """The real `Ops.alert` through the real mailbox library into a throwaway inbox
    (a path not under the hub, so mailbox.send keeps its sent-ledger untouched)."""

    def setUp(self):
        from tools import mailbox
        self.mailbox = mailbox
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.inbox = Path(self._td.name) / "orchestrator"

    def test_alert_lands_in_orchestrators_mailbox(self):
        self.mailbox.provision(self.inbox)
        with patch.dict("os.environ", {"PANTHEON_ORCHESTRATOR_INBOX": str(self.inbox)}):
            nw.Ops().alert("nightly CI RED: run 1 failure", "the body text")
        (msg,) = list((self.inbox / "new").iterdir())
        record = json.loads(msg.read_text())
        self.assertEqual(record["sender"], nw.SENDER)
        self.assertEqual(record["subject"], "nightly CI RED: run 1 failure")
        self.assertEqual(record["payload"], "the body text")
        self.assertEqual(record["severity"], "normal")

    def test_an_absent_mailbox_makes_delivery_fail_loudly_and_unmarked(self):
        class RealAlertOps(FakeOps):
            alert = nw.Ops.alert
        ops = RealAlertOps([run(1, "failure", 1)], jobs=[], logs={})
        with patch.dict("os.environ", {"PANTHEON_ORCHESTRATOR_INBOX": str(self.inbox)}), \
                contextlib.redirect_stderr(io.StringIO()), \
                tempfile.TemporaryDirectory() as state:
            result = nw.check_once(ops=ops, state_dir=Path(state))
            self.assertTrue(result["delivery_failed"])
            self.assertEqual(result["alerted"], [])
            saved = json.loads((Path(state) / "ci-nightly-watch.json").read_text())
            self.assertEqual(saved["alerted_runs"], {})

    def test_default_recipient_is_orchestrator_under_the_configured_hub(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PANTHEON_ORCHESTRATOR_INBOX", None)
            self.assertTrue(nw.orchestrator_inbox().endswith("/orchestrator"))


class RedRunTests(TmpStateCase):
    def _red_ops(self, **kw):
        jobs = [job(11, "tools suite (py3.12)"), job(12, "tools suite (py3.11)")]
        logs = {11: REAL_PYTEST_LOG, 12: REAL_PYTEST_LOG + REAL_CHECKER_LOG}
        return FakeOps([run(36326511096, "failure", 1.5)], jobs=jobs, logs=logs, **kw)

    def test_red_run_mails_orchestrator_once_with_the_failed_tests(self):
        ops = self._red_ops()
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.RED)
        self.assertEqual(result["alerted"], [nw.RED])
        self.assertEqual(len(ops.sent), 1)
        subject, body = ops.sent[0]
        self.assertIn("RED", subject)
        self.assertIn("36326511096", subject)
        self.assertIn("https://github.com/x/y/actions/runs/36326511096", body)
        self.assertIn("ca90576e4", body)
        self.assertIn("tools suite (py3.11)", body)
        self.assertIn("Run tools test suite", body)
        self.assertIn("test_initial_backfill_never_infers_project_from_repo_or_persona", body)
        self.assertIn("4 failed, 10273 passed", body)
        self.assertIn("check_shell_suite_outcomes: 17 problem(s)", body)
        self.assertIn("check_suite_outcome_floor:", body)
        self.assertEqual(self.state_json()["alerted_runs"].keys(), {"36326511096.1"})

    def test_the_same_run_is_never_mailed_twice(self):
        ops = self._red_ops()
        self.check(ops)
        again = self.check(ops)
        self.assertEqual(len(ops.sent), 1)
        self.assertEqual(again["alerted"], [])
        self.assertEqual(again["verdict"], nw.RED)  # still red, just already told

    def test_a_rerun_attempt_that_fails_again_is_a_new_alert(self):
        ops = self._red_ops()
        self.check(ops)
        ops.runs = [run(36326511096, "failure", 1.0, attempt=2)]
        self.check(ops)
        self.assertEqual(len(ops.sent), 2)
        self.assertEqual(set(self.state_json()["alerted_runs"]), {"36326511096.1", "36326511096.2"})

    def test_timed_out_and_startup_failure_are_red(self):
        for conclusion in ("timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                ops = FakeOps([run(7, conclusion, 1)], jobs=[])
                result = self.check(ops, )
                self.assertEqual(result["verdict"], nw.RED)
                self.assertIn(conclusion, ops.sent[0][1])
                self.assertIn("no job logs", ops.sent[0][1])
                (self.state / "ci-nightly-watch.json").unlink()

    def test_red_alert_survives_job_and_log_read_failures(self):
        for kw in ({"jobs_error": "jobs 502"}, {"log_error": "log 403"}):
            with self.subTest(kw=kw):
                ops = FakeOps([run(8, "failure", 1)], jobs=[job(1, "tools suite (py3.12)")],
                              logs={1: REAL_PYTEST_LOG}, **kw)
                result = self.check(ops)
                self.assertEqual(result["verdict"], nw.RED)
                self.assertEqual(len(ops.sent), 1)
                self.assertIn("unavailable", ops.sent[0][1])
                (self.state / "ci-nightly-watch.json").unlink()

    def test_failed_delivery_is_not_marked_sent_and_is_retried(self):
        ops = self._red_ops(send_fails=1)
        first = self.check(ops)
        self.assertTrue(first["delivery_failed"])
        self.assertEqual(first["alerted"], [])
        self.assertEqual(ops.sent, [])
        self.assertNotIn("36326511096.1", self.state_json().get("alerted_runs", {}))
        second = self.check(ops)
        self.assertFalse(second["delivery_failed"])
        self.assertEqual(second["alerted"], [nw.RED])
        self.assertEqual(len(ops.sent), 1)

    def test_success_and_in_flight_runs_do_not_alert(self):
        ops = FakeOps([run(2, "success", 2)])
        self.assertEqual(self.check(ops)["verdict"], nw.OK)
        ops = FakeOps([run(3, None, 1, status="in_progress"), run(2, "success", 25)])
        self.assertEqual(self.check(ops)["verdict"], nw.RUNNING)
        self.assertEqual(ops.sent, [])


class UnmeasurableTests(TmpStateCase):
    def test_a_read_error_alerts_unknown_not_ok(self):
        ops = FakeOps(error="gh exited 1: not logged in")
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.UNKNOWN)
        self.assertEqual(result["alerted"], [nw.UNKNOWN])
        self.assertIn("not logged in", ops.sent[0][1])
        self.assertIn("NOT a green", ops.sent[0][1])

    def test_unparseable_created_at_and_unknown_conclusion_alert_unknown(self):
        bad = run(4, "success", 1)
        bad["created_at"] = "yesterday-ish"
        self.assertEqual(self.check(FakeOps([bad]))["verdict"], nw.UNKNOWN)
        (self.state / "ci-nightly-watch.json").unlink()
        odd = FakeOps([run(5, "action_required", 1)])
        self.assertEqual(self.check(odd)["verdict"], nw.UNKNOWN)
        self.assertEqual(len(odd.sent), 1)

    def test_unknown_repeats_only_after_the_cooldown(self):
        ops = FakeOps(error="boom")
        self.check(ops)
        self.check(ops)
        self.assertEqual(len(ops.sent), 1)
        ops._now = NOW + 12 * HOUR + 1
        self.check(ops)
        self.assertEqual(len(ops.sent), 2)


class MissingRunTests(TmpStateCase):
    def test_no_run_created_in_36h_is_a_dead_schedule(self):
        ops = FakeOps([run(1, "success", 37)])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.DEAD_SCHEDULE)
        self.assertEqual(result["alerted"], [nw.DEAD_SCHEDULE])
        self.assertIn("37", ops.sent[0][1])

    def test_a_run_inside_the_window_is_not(self):
        # 29h is the observed worst NORMAL gap (creation 12:33Z one day, 17:27Z the next)
        ops = FakeOps([run(1, "success", 35)])
        self.assertEqual(self.check(ops)["verdict"], nw.OK)
        self.assertEqual(ops.sent, [])

    def test_dead_schedule_cooldown_then_repeat(self):
        ops = FakeOps([run(1, "success", 40)])
        self.check(ops)
        self.check(ops)
        self.assertEqual(len(ops.sent), 1)
        ops._now = NOW + 12 * HOUR + 1
        self.check(ops)
        self.assertEqual(len(ops.sent), 2)

    def test_a_run_stuck_queued_or_running_past_8h_alerts(self):
        stuck = FakeOps([run(9, None, 9, status="queued"), run(8, "success", 30)])
        result = self.check(stuck)
        self.assertEqual(result["verdict"], nw.PENDING_STUCK)
        self.assertEqual(len(stuck.sent), 1)
        fine = FakeOps([run(9, None, 7, status="queued"), run(8, "success", 30)])
        self.assertEqual(self.check(fine)["verdict"], nw.RUNNING)
        self.assertEqual(fine.sent, [])

    def test_an_already_told_red_does_not_mask_a_dead_schedule(self):
        ops = FakeOps([run(6, "failure", 40)], jobs=[job(1, "tools suite (py3.12)")],
                      logs={1: REAL_PYTEST_LOG})
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.RED)
        self.assertEqual(sorted(result["alerted"]), sorted([nw.RED, nw.DEAD_SCHEDULE]))
        self.assertEqual(len(ops.sent), 2)
        again = self.check(ops)
        self.assertEqual(again["alerted"], [])


MARK_ID = 36889607146
STALE_ID = 36738951955


def stale_listing():
    # the 10-02 / 10-05 shape: the newest in the read is from 09-30, older than the mark
    return [run(STALE_ID, "cancelled", 44), run(36458276520, "cancelled", 70)]


class StaleReadTests(TmpStateCase):
    def seed(self, mark_ago_h=20, run_id=MARK_ID, raw=None):
        mark = raw if raw is not None else {"run_id": run_id, "created_at": iso(mark_ago_h * HOUR)}
        (self.state / "ci-nightly-watch.json").write_text(json.dumps({"high_water": mark}))

    def test_stale_listing_is_silent_not_dead_schedule_or_inconclusive(self):
        self.seed()
        ops = FakeOps(stale_listing())
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.STALE_READ)
        self.assertEqual(ops.sent, [])
        self.assertEqual(result["alerted"], [])
        self.assertNotIn(nw.DEAD_SCHEDULE, result["conditions"])
        self.assertNotIn(nw.INCONCLUSIVE, result["conditions"])
        self.assertEqual(ops.reads, 2)
        self.assertEqual(ops.sleeps, [nw.DEFAULT_STALE_REQUERY_SECS])

    def test_requery_recovers_and_mark_advances(self):
        self.seed()
        fresh = [run(MARK_ID + 100, "success", 3), run(MARK_ID, "success", 20)]
        ops = FakeOps(runs_seq=[stale_listing(), fresh])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.OK)
        self.assertEqual(ops.sent, [])
        hw = self.state_json()["high_water"]
        self.assertEqual(hw["run_id"], MARK_ID + 100)
        self.assertEqual(self.state_json().get("stale_reads", 0), 0)

    def test_mark_too_old_still_alerts_dead_schedule_judged_from_mark(self):
        self.seed(mark_ago_h=40)
        ops = FakeOps(stale_listing())
        result = self.check(ops)
        self.assertEqual(result["alerted"], [nw.DEAD_SCHEDULE])
        self.assertEqual(result["verdict"], nw.DEAD_SCHEDULE)
        self.assertIn(str(MARK_ID), ops.sent[0][1])
        self.assertIn("40.0h", ops.sent[0][1])

    def test_corrupt_state_file_behaves_as_before(self):
        (self.state / "ci-nightly-watch.json").write_text("{not json")
        ops = FakeOps([run(STALE_ID, "success", 44)])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.DEAD_SCHEDULE)
        self.assertEqual(ops.reads, 1)

    def test_invalid_mark_types_are_no_mark(self):
        self.seed(raw={"run_id": "abc", "created_at": "garbage"})
        ops = FakeOps([run(STALE_ID, "success", 44)])
        self.assertEqual(self.check(ops)["verdict"], nw.DEAD_SCHEDULE)

    def test_mark_only_moves_forward_and_persists(self):
        ops = FakeOps([run(500, "success", 3)])
        self.check(ops)
        self.assertEqual(self.state_json()["high_water"]["run_id"], 500)
        self.check(FakeOps([run(500, "success", 3), run(400, "success", 27)]))
        self.assertEqual(self.state_json()["high_water"]["run_id"], 500)
        self.check(FakeOps([run(600, "success", 2)]))
        self.assertEqual(self.state_json()["high_water"]["run_id"], 600)
        # a stale read never lowers or rewrites it
        self.check(FakeOps([run(550, "success", 2)]))
        self.assertEqual(self.state_json()["high_water"]["run_id"], 600)

    def test_requery_zero_disables_the_retry_but_still_detects(self):
        self.seed()
        ops = FakeOps(stale_listing())
        result = self.check(ops, stale_requery_secs=0)
        self.assertEqual(result["verdict"], nw.STALE_READ)
        self.assertEqual(ops.reads, 1)
        self.assertEqual(ops.sleeps, [])

    def test_dry_run_writes_no_mark(self):
        ops = FakeOps([run(500, "success", 3)])
        self.check(ops, dry_run=True)
        self.assertFalse((self.state / "ci-nightly-watch.json").exists())

    def test_requery_error_keeps_stale_read(self):
        self.seed()
        ops = FakeOps(runs_seq=[stale_listing(), nw.GhError("boom")])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.STALE_READ)
        self.assertEqual(ops.sent, [])

    def test_consecutive_stale_count_in_state_and_detail_then_resets(self):
        self.seed()
        r1 = self.check(FakeOps(stale_listing()))
        self.assertEqual(self.state_json()["stale_reads"], 1)
        r2 = self.check(FakeOps(stale_listing()))
        self.assertEqual(self.state_json()["stale_reads"], 2)
        self.assertIn("2 consecutive", r2["detail"])
        self.assertIn("1 consecutive", r1["detail"])
        self.check(FakeOps([run(MARK_ID + 5, "success", 2)]))
        self.assertEqual(self.state_json()["stale_reads"], 0)

    def test_concurrent_probe_newer_mark_on_disk_is_not_regressed(self):
        self.seed(run_id=100)
        sp = self.state / "ci-nightly-watch.json"
        newer = {"run_id": 200, "created_at": iso(1 * HOUR)}

        class Racing(FakeOps):
            def scheduled_runs(self_inner):
                # another probe persists a newer mark after we loaded state, before we write
                sp.write_text(json.dumps({"high_water": newer}))
                return super().scheduled_runs()

        self.check(Racing([run(150, "success", 3)]))
        self.assertEqual(self.state_json()["high_water"]["run_id"], 200)
        self.assertFalse(list(self.state.glob("*.tmp")))


class CancelledRunTests(TmpStateCase):
    def test_a_single_cancelled_nightly_is_recorded_but_silent(self):
        ops = FakeOps([run(3, "cancelled", 2), run(2, "success", 26)])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.CANCELLED)
        self.assertEqual(ops.sent, [])
        self.assertEqual(self.state_json()["last_verdict"], nw.CANCELLED)

    def test_two_consecutive_cancelled_nightlies_alert_inconclusive_with_cooldown(self):
        ops = FakeOps([run(3, "cancelled", 2), run(2, "cancelled", 26), run(1, "success", 50)])
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.INCONCLUSIVE)
        self.assertEqual(result["alerted"], [nw.INCONCLUSIVE])
        self.check(ops)
        self.assertEqual(len(ops.sent), 1)
        ops._now = NOW + 12 * HOUR + 1
        ops.runs = [run(3, "cancelled", 14), run(2, "cancelled", 38)]
        self.check(ops, dead_secs=99 * HOUR)
        self.assertEqual(len(ops.sent), 2)

    def test_cancelled_then_red_history_is_not_inconclusive(self):
        ops = FakeOps([run(3, "cancelled", 2), run(2, "failure", 26)])
        self.assertEqual(self.check(ops)["verdict"], nw.CANCELLED)


class MuteAndDryRunTests(TmpStateCase):
    def _mute(self, until_secs_from_now):
        until = datetime.fromtimestamp(NOW + until_secs_from_now, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        (self.state / "ci-nightly-watch.disabled").write_text(
            json.dumps({"reason": "GitHub Actions incident", "until": until}))

    def test_an_active_mute_silences_even_a_red_run(self):
        self._mute(6 * HOUR)
        ops = FakeOps([run(1, "failure", 1)], jobs=[job(1, "tools suite (py3.12)")],
                      logs={1: REAL_PYTEST_LOG})
        result = self.check(ops)
        self.assertEqual(result["verdict"], nw.DISABLED)
        self.assertIn("GitHub Actions incident", result["detail"])
        self.assertEqual(ops.sent, [])

    def test_an_expired_mute_does_not_apply(self):
        self._mute(-HOUR)
        ops = FakeOps([run(1, "failure", 1)], jobs=[], logs={})
        self.assertEqual(self.check(ops)["verdict"], nw.RED)
        self.assertEqual(len(ops.sent), 1)

    def test_dry_run_decides_but_never_sends_or_writes_state(self):
        ops = FakeOps([run(1, "failure", 1)], jobs=[job(1, "tools suite (py3.12)")],
                      logs={1: REAL_PYTEST_LOG})
        result = self.check(ops, dry_run=True)
        self.assertEqual(result["verdict"], nw.RED)
        self.assertEqual(result["alerted"], [])
        self.assertEqual(result["would_alert"], [nw.RED])
        self.assertEqual(ops.sent, [])
        self.assertFalse((self.state / "ci-nightly-watch.json").exists())

    def test_alerted_run_history_is_bounded(self):
        for i in range(nw.MAX_ALERTED_RUNS + 10):
            ops = FakeOps([run(1000 + i, "failure", 1)], jobs=[], logs={})
            self.check(ops)
        kept = self.state_json()["alerted_runs"]
        self.assertEqual(len(kept), nw.MAX_ALERTED_RUNS)
        self.assertIn(f"{1000 + nw.MAX_ALERTED_RUNS + 9}.1", kept)
        self.assertNotIn("1000.1", kept)


class MainTests(TmpStateCase):
    def _main(self, ops, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(nw, "Ops", lambda: ops), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = nw.main(["--state-dir", str(self.state), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_exit_0_on_an_alerting_verdict_that_delivered(self):
        rc, _, err = self._main(FakeOps(error="boom"))
        self.assertEqual(rc, 0)
        self.assertIn("UNKNOWN", err)

    def test_exit_1_when_the_alert_could_not_be_delivered(self):
        # non-zero so the unit's OnFailure fires: a dead alert path must be loud
        rc, _, err = self._main(FakeOps(error="boom", send_fails=1))
        self.assertEqual(rc, 1)
        self.assertIn("delivery FAILED", err)

    def test_json_output_and_dry_run_flag(self):
        rc, out, _ = self._main(FakeOps([run(1, "success", 2)]), "--json", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["verdict"], nw.OK)


if __name__ == "__main__":
    unittest.main()
