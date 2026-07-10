#!/usr/bin/env python3
"""Tests for tools/knowledge_sweep.py (ORG-012 Phase C, T-0781 component 1)."""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import _git_utils, ticket
from tools import knowledge_sweep as ks


# ---------------------------------------------------------------------------
# Fingerprint + diff.
# ---------------------------------------------------------------------------


class TestFingerprint(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "lessons").mkdir(parents=True)
        (self.root / "bugs").mkdir(parents=True)
        (self.root / "lessons" / "a.md").write_text("alpha", encoding="utf-8")
        (self.root / "bugs" / "b.md").write_text("beta", encoding="utf-8")
        # A non-corpus dir + a non-md file must be ignored.
        (self.root / "tickets").mkdir()
        (self.root / "tickets" / "T-1.md").write_text("x", encoding="utf-8")
        (self.root / "lessons" / "notes.txt").write_text("y", encoding="utf-8")
        self.mem = self.root / "memory"
        self.mem.mkdir()
        (self.mem / "feedback_one.md").write_text("pin", encoding="utf-8")
        (self.mem / "user_x.md").write_text("not-a-pin", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_covers_corpus_md_and_feedback_pins_only(self) -> None:
        fp = ks.compute_corpus_fingerprint(self.root, self.mem)
        self.assertEqual(
            set(fp),
            {"lessons/a.md", "bugs/b.md", "memory:feedback_one.md"},
        )

    def test_recurses_subdirs(self) -> None:
        nested = self.root / "sop" / "remediation"
        nested.mkdir(parents=True)
        (nested / "r.md").write_text("z", encoding="utf-8")
        fp = ks.compute_corpus_fingerprint(self.root, self.mem)
        self.assertIn("sop/remediation/r.md", fp)

    def test_excludes_high_churn_watch_reports(self) -> None:
        # knowledge/watch/reports/ (daily churn) excluded; knowledge/episodic/ kept.
        rpt = self.root / "knowledge" / "watch" / "reports" / "2026-06-06"
        rpt.mkdir(parents=True)
        (rpt / "topic.md").write_text("daily watch output", encoding="utf-8")
        epi = self.root / "knowledge" / "episodic" / "2026-06-01-study"
        epi.mkdir(parents=True)
        (epi / "notes.md").write_text("research synthesis", encoding="utf-8")
        fp = ks.compute_corpus_fingerprint(self.root, self.mem)
        self.assertNotIn("knowledge/watch/reports/2026-06-06/topic.md", fp)
        self.assertIn("knowledge/episodic/2026-06-01-study/notes.md", fp)

    def test_missing_memory_dir_is_skipped_not_error(self) -> None:
        fp = ks.compute_corpus_fingerprint(self.root, self.root / "nope")
        self.assertNotIn("memory:feedback_one.md", fp)
        self.assertIn("lessons/a.md", fp)

    def test_diff_added_changed_removed(self) -> None:
        old = {"a": "1", "b": "2", "c": "3"}
        new = {"a": "1", "b": "CHANGED", "d": "4"}
        diff = ks.diff_fingerprint(old, new)
        self.assertEqual(diff["added"], ["d"])
        self.assertEqual(diff["changed"], ["b"])
        self.assertEqual(diff["removed"], ["c"])

    def test_hash_is_content_based(self) -> None:
        fp1 = ks.compute_corpus_fingerprint(self.root, self.mem)
        (self.root / "lessons" / "a.md").write_text("alpha-edited", encoding="utf-8")
        fp2 = ks.compute_corpus_fingerprint(self.root, self.mem)
        self.assertNotEqual(fp1["lessons/a.md"], fp2["lessons/a.md"])
        self.assertEqual(fp1["bugs/b.md"], fp2["bugs/b.md"])


# ---------------------------------------------------------------------------
# file_sweep_ticket: the gate + idempotent filer.
# ---------------------------------------------------------------------------


class TestFileSweepTicket(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "tickets" / "inbox").mkdir(parents=True)
        self._patches = [
            patch.object(ks, "REPO_ROOT", self.tmp),
            patch.object(ks, "_append_runner_log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _state(self, fp: dict) -> dict:
        return {"schema_version": 1, "corpus_fingerprint": fp, "last_sweep": None}

    def test_no_ticket_when_corpus_unchanged(self) -> None:
        fp = {"lessons/a.md": "h1"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=fp), \
             patch.object(ks, "load_state", return_value=self._state(fp)), \
             patch.object(ks, "_invoke_ticket_file") as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)

    def test_files_ticket_when_corpus_changed(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2", "bugs/new.md": "h3"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state", return_value=self._state(old)), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("filed", "T-9001", "retries=0")) as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)
        argv = mock_file.call_args.args[0]
        self.assertEqual(argv[argv.index("--target") + 1], "specialist")
        self.assertEqual(argv[argv.index("--requester") + 1], "specialist")
        # ticket.py rejects --intent for non-operator targets, so a target=specialist
        # dispatch ticket must NOT carry it (regression lock for the 2026-06-06
        # 13:00 live-fire failure, which mocked unit tests had missed).
        self.assertNotIn("--intent", argv)
        ask = argv[argv.index("--ask") + 1]
        # Carries the team-SOP recipe + the SOP-shaped output contract + the changed paths.
        self.assertIn("team-orchestrator-dispatch-checklist", ask)
        self.assertIn("throughline-sweep-result-filing", ask)
        self.assertIn("bugs/new.md", ask)
        context = argv[argv.index("--context") + 1]
        self.assertIn("knowledge-sweep-dispatch: 2026-06-06", context)

    def test_idempotent_skip_when_dispatch_pending(self) -> None:
        (self.tmp / "tickets" / "inbox" / "T-9000.md").write_text(
            "---\nid: T-9000\n---\n\nknowledge-sweep-dispatch: 2026-06-05\n",
            encoding="utf-8",
        )
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state", return_value=self._state(old)), \
             patch.object(ks, "_invoke_ticket_file") as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)

    def test_force_files_even_when_unchanged(self) -> None:
        fp = {"lessons/a.md": "h1"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=fp), \
             patch.object(ks, "load_state", return_value=self._state(fp)), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("filed", "T-9002", "retries=0")) as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 6), force=True)
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)

    def test_filing_failure_returns_nonzero(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state", return_value=self._state(old)), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("errored", None, "retries=2; stderr=git push")), \
             patch("sys.stderr", new_callable=io.StringIO):
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Day-index integrity check (T-1127): the daily sweep surfaces watch-report
# day-index drift via the T-1121 lint, but NEVER fails on it.
# ---------------------------------------------------------------------------


class TestDayIndexDriftCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Temp REPO_ROOT carrying a symlink to the REAL lint so the check runs
        # end-to-end (subprocess) against a controlled reports tree.
        real_lint = Path(ks.__file__).resolve().parent / "lint_report_day_index.py"
        (self.tmp / "tools").mkdir(parents=True)
        (self.tmp / "tools" / "lint_report_day_index.py").symlink_to(real_lint)
        self.reports = self.tmp / "knowledge" / "watch" / "reports"
        self.reports.mkdir(parents=True)
        self._patch_root = patch.object(ks, "REPO_ROOT", self.tmp)
        self._patch_root.start()

    def tearDown(self) -> None:
        self._patch_root.stop()
        self._tmp.cleanup()

    def _day(self, name: str, files: dict, readme: str) -> None:
        d = self.reports / name
        d.mkdir(parents=True)
        for fn, body in files.items():
            (d / fn).write_text(body, encoding="utf-8")
        (d / "README.md").write_text(readme, encoding="utf-8")

    def test_clean_tree_stays_quiet(self) -> None:
        self._day(
            "2026-01-02",
            {"a.md": "# a\n"},
            "# Day\n\n| Topic |\n|---|\n| [a](a.md) |\n",
        )
        with patch.object(ks, "_append_runner_log") as log, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            ks._check_day_index_drift()
        self.assertFalse(log.called)
        self.assertEqual(err.getvalue(), "")

    def test_drift_surfaces_to_log_and_stderr(self) -> None:
        # orphan-report.md on disk but not linked in README → UNINDEXED drift
        self._day(
            "2026-01-03",
            {"orphan-report.md": "# orphan\n"},
            "# Day\n\n| Topic |\n|---|\n",
        )
        with patch.object(ks, "_append_runner_log") as log, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            ks._check_day_index_drift()
        self.assertTrue(log.called)
        self.assertIn("day-index-lint: DRIFT", log.call_args.args[0])
        self.assertIn("orphan-report.md", err.getvalue())

    def test_missing_lint_bin_is_safe(self) -> None:
        # No lint on disk → early-return; never crash, never log (best-effort).
        (self.tmp / "tools" / "lint_report_day_index.py").unlink()
        self._day("2026-01-04", {"x.md": "# x\n"}, "# Day\n")  # would drift if it ran
        with patch.object(ks, "_append_runner_log") as log, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            ks._check_day_index_drift()
        self.assertFalse(log.called)
        self.assertEqual(err.getvalue(), "")

    def test_never_raises_on_lint_error(self) -> None:
        # A lint invocation that blows up must be swallowed, not propagated.
        with patch.object(ks.subprocess, "run", side_effect=OSError("boom")), \
             patch.object(ks, "_append_runner_log") as log, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            ks._check_day_index_drift()  # must not raise
        self.assertFalse(log.called)
        self.assertIn("skipped", err.getvalue())


# ---------------------------------------------------------------------------
# MIN_SWEEP_INTERVAL_DAYS floor (T-0974): defer filing when a sweep landed
# fewer than the floor's days ago, even if the corpus changed.
# ---------------------------------------------------------------------------


class TestMinSweepIntervalFloor(unittest.TestCase):
    def test_helper_default_when_env_absent(self) -> None:
        self.assertEqual(ks.min_sweep_interval_days({}), 7)

    def test_helper_env_override(self) -> None:
        self.assertEqual(ks.min_sweep_interval_days({"MIN_SWEEP_INTERVAL_DAYS": "3"}), 3)

    def test_helper_blank_and_bad_fall_back_to_default(self) -> None:
        self.assertEqual(ks.min_sweep_interval_days({"MIN_SWEEP_INTERVAL_DAYS": "  "}), 7)
        self.assertEqual(ks.min_sweep_interval_days({"MIN_SWEEP_INTERVAL_DAYS": "nope"}), 7)
        self.assertEqual(ks.min_sweep_interval_days({"MIN_SWEEP_INTERVAL_DAYS": "-4"}), 7)

    def test_helper_zero_disables(self) -> None:
        self.assertEqual(ks.min_sweep_interval_days({"MIN_SWEEP_INTERVAL_DAYS": "0"}), 0)

    def test_within_interval_none_when_no_prior_sweep(self) -> None:
        self.assertIsNone(ks.within_min_interval(None, date(2026, 6, 6), {}))

    def test_within_interval_defers_when_recent(self) -> None:
        # landed 2d ago, default 7d floor → defer
        self.assertEqual(
            ks.within_min_interval("2026-06-04", date(2026, 6, 6), {}), (2, 7)
        )

    def test_within_interval_allows_when_satisfied(self) -> None:
        # landed 8d ago, default 7d floor → no defer
        self.assertIsNone(ks.within_min_interval("2026-05-29", date(2026, 6, 6), {}))

    def test_within_interval_fails_open_on_bad_date(self) -> None:
        self.assertIsNone(ks.within_min_interval("not-a-date", date(2026, 6, 6), {}))

    def test_within_interval_disabled_floor_never_defers(self) -> None:
        self.assertIsNone(
            ks.within_min_interval("2026-06-05", date(2026, 6, 6), {"MIN_SWEEP_INTERVAL_DAYS": "0"})
        )


class TestFloorInFileSweepTicket(TestFileSweepTicket):
    """Reuses TestFileSweepTicket's tmp-repo + empty-inbox + log-patch setUp."""

    def _state_landed(self, fp: dict, last_sweep: str) -> dict:
        return {"schema_version": 1, "corpus_fingerprint": fp, "last_sweep": last_sweep}

    def test_defers_when_within_floor(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}  # corpus changed
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state",
                          return_value=self._state_landed(old, "2026-06-04")), \
             patch.dict("os.environ", {}, clear=False), \
             patch.object(ks, "_invoke_ticket_file") as mock_file:
            # today is 2026-06-06 → 2d since last sweep < 7d default floor
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)

    def test_files_when_floor_satisfied(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state",
                          return_value=self._state_landed(old, "2026-05-29")), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("filed", "T-9100", "retries=0")) as mock_file:
            # 2026-06-06 is 8d after 2026-05-29 → >= 7d floor
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)

    def test_force_bypasses_floor(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state",
                          return_value=self._state_landed(old, "2026-06-05")), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("filed", "T-9101", "retries=0")) as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 6), force=True)
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)

    def test_env_zero_disables_floor(self) -> None:
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2"}
        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state",
                          return_value=self._state_landed(old, "2026-06-05")), \
             patch.dict("os.environ", {"MIN_SWEEP_INTERVAL_DAYS": "0"}), \
             patch.object(ks, "_invoke_ticket_file",
                          return_value=("filed", "T-9102", "retries=0")) as mock_file:
            # 1d since last sweep, but floor disabled → files
            rc = ks.file_sweep_ticket(date(2026, 6, 6))
        self.assertEqual(rc, 0)
        self.assertTrue(mock_file.called)


# ---------------------------------------------------------------------------
# Integration smoke: the argv file_sweep_ticket builds must survive ticket.py's
# REAL argument layer.
#
# The TestFileSweepTicket cases above mock _invoke_ticket_file, so they assert
# what argv we *construct* but never run it through ticket.py's actual parser +
# cmd_file validation. That mock-hides-integration gap is exactly what let the
# 2026-06-06 13:00 live-fire failure ship: the argv carried `--intent action`,
# which ticket.py refuses for any non-operator target, and no test caught it
# because the boundary was mocked on both sides.
#
# This class closes the gap WITHOUT a real subprocess / network: it captures the
# production argv (via the same mock), then replays the `file ...` tail through
# ticket.main() IN-PROCESS against a temp REPO_ROOT with git stubbed. ticket.py's
# real arg-validation, tid allocation, file write, and scope guard all run; only
# the push is skipped (--no-push). If the dispatch argv ever drifts back to a
# shape ticket.py rejects for target=specialist, test_production_argv_accepted fails.
# ---------------------------------------------------------------------------


class TestSweepArgvAcceptedByRealTicketPy(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for s in ticket.STATES:
            (self.root / "tickets" / s).mkdir(parents=True, exist_ok=True)
        # Minimal board so cmd_file's README rebuild + scope guard have a target.
        (self.root / "tickets" / "README.md").write_text(
            "# Tickets\n\n## Current state\n\n"
            "| ID | State | Title | Requester → Target | Filed |\n"
            "|---|---|---|---|---|\n\n## Related\n\n- Link\n",
            encoding="utf-8",
        )
        self._patches = [
            patch.object(ticket, "REPO_ROOT", self.root),
            patch.object(ticket, "TICKETS_ROOT", self.root / "tickets"),
            patch.object(_git_utils, "REPO_ROOT", self.root),
        ]
        for p in self._patches:
            p.start()

        # Git stub: emulate just enough of add / diff --cached / commit for the
        # scope guard to pass when the staged set == [ticket file, README]. No
        # real git, no network. --no-push means fetch/pull/push are never reached.
        self._staged: set[str] = set()

        def fake_run(argv, **kwargs):
            if argv[:2] == ["git", "add"] and "--" in argv:
                for p in argv[argv.index("--") + 1:]:
                    if (self.root / p).exists():
                        self._staged.add(p)
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:2] == ["git", "diff"]:
                out = "\n".join(sorted(self._staged))
                return MagicMock(
                    returncode=0, stdout=out + ("\n" if out else ""), stderr=""
                )
            if argv[:2] == ["git", "commit"]:
                self._staged.clear()
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        self._sub_patch = patch(
            "tools._git_utils.subprocess.run", side_effect=fake_run
        )
        self._sub_patch.start()

    def tearDown(self) -> None:
        self._sub_patch.stop()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _production_argv(self) -> list[str]:
        """Capture the exact argv file_sweep_ticket hands to ticket.py."""
        old = {"lessons/a.md": "h1"}
        new = {"lessons/a.md": "h2", "bugs/new.md": "h3"}
        captured: dict[str, list[str]] = {}

        def cap(argv, *a, **k):
            captured["argv"] = argv
            return ("filed", "T-9001", "retries=0")

        with patch.object(ks, "compute_corpus_fingerprint", return_value=new), \
             patch.object(ks, "load_state", return_value={
                 "schema_version": 1, "corpus_fingerprint": old, "last_sweep": None}), \
             patch.object(ks, "pending_dispatch_in_inbox", return_value=None), \
             patch.object(ks, "_append_runner_log"), \
             patch.object(ks, "_invoke_ticket_file", side_effect=cap):
            ks.file_sweep_ticket(date(2026, 6, 6))
        return captured["argv"]

    def test_production_argv_accepted_by_real_ticket_py(self) -> None:
        # argv == [python, ticket_bin, "file", ...]; replay the "file ..." tail.
        argv = self._production_argv()
        rc = ticket.main(argv[2:] + ["--no-push"])
        self.assertEqual(
            rc, 0,
            "ticket.py rejected the sweep dispatch argv — arg drift like the "
            "2026-06-06 --intent regression. Check file_sweep_ticket's argv.",
        )
        inbox = list((self.root / "tickets" / "inbox").glob("T-*.md"))
        self.assertEqual(len(inbox), 1, "dispatch ticket did not land in inbox")

    def test_intent_injection_is_caught_by_this_smoke(self) -> None:
        # Guard-the-guard: re-add --intent (the original bug shape) and confirm
        # this smoke would have failed it — i.e. the test has teeth.
        argv = self._production_argv()
        tail = list(argv[2:])
        i = tail.index("--target")
        bad = tail[:i] + ["--intent", "action"] + tail[i:]
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = ticket.main(bad + ["--no-push"])
        self.assertEqual(rc, 1)
        self.assertIn("--intent is target=operator-specific", err.getvalue())


# ---------------------------------------------------------------------------
# Candidate identity + diff + land_sweep.
# ---------------------------------------------------------------------------


class TestCandidates(unittest.TestCase):
    def test_key_stable_across_rationale_drift(self) -> None:
        c1 = {"type": "promotion", "sources": ["a.md", "b.md"], "target_tier": "sop",
              "rationale": "first wording", "priority": "HIGH"}
        c2 = {"type": "promotion", "sources": ["b.md", "a.md"], "target_tier": "sop",
              "rationale": "totally different wording", "priority": "MED"}
        self.assertEqual(ks.candidate_key(c1), ks.candidate_key(c2))

    def test_key_differs_on_target_or_type(self) -> None:
        base = {"type": "promotion", "sources": ["a.md"], "target_tier": "sop"}
        self.assertNotEqual(
            ks.candidate_key(base),
            ks.candidate_key({**base, "target_tier": "directive"}),
        )
        self.assertNotEqual(
            ks.candidate_key(base),
            ks.candidate_key({**base, "type": "demotion"}),
        )

    def test_new_candidates_filters_seen(self) -> None:
        a = {"type": "promotion", "sources": ["a.md"], "target_tier": "sop"}
        b = {"type": "demotion", "sources": ["b.md"], "target_tier": ""}
        seen = {ks.candidate_key(a)}
        fresh = ks.new_candidates([a, b], seen)
        self.assertEqual(fresh, [b])


class TestLandSweep(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "lessons").mkdir(parents=True)
        (self.root / "lessons" / "a.md").write_text("alpha", encoding="utf-8")
        self.state_file = self.root / "state.json"
        self._patches = [
            patch.object(ks, "STATE_FILE", self.state_file),
            patch.object(ks, "STATE_DIR", self.root),
            patch.object(ks, "_append_runner_log"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_land_persists_fingerprint_and_candidates(self) -> None:
        cands = [{"type": "promotion", "sources": ["lessons/a.md"], "target_tier": "sop"}]
        rc = ks.land_sweep(date(2026, 6, 6), cands,
                           repo_root=self.root, memory_dir=self.root / "nope")
        self.assertEqual(rc, 0)
        state = ks.load_state()
        self.assertIn("lessons/a.md", state["corpus_fingerprint"])
        self.assertEqual(state["last_candidates"], [ks.candidate_key(cands[0])])
        self.assertEqual(state["last_sweep"], "2026-06-06")

    def test_land_unions_candidate_keys_across_runs(self) -> None:
        c1 = {"type": "promotion", "sources": ["lessons/a.md"], "target_tier": "sop"}
        c2 = {"type": "demotion", "sources": ["lessons/a.md"], "target_tier": ""}
        ks.land_sweep(date(2026, 6, 6), [c1], repo_root=self.root, memory_dir=self.root / "x")
        ks.land_sweep(date(2026, 6, 7), [c2], repo_root=self.root, memory_dir=self.root / "x")
        state = ks.load_state()
        self.assertEqual(
            set(state["last_candidates"]),
            {ks.candidate_key(c1), ks.candidate_key(c2)},
        )

    def test_land_gate_quiets_after_landing(self) -> None:
        # After landing, file_sweep_ticket on an unchanged corpus is a no-op.
        cands = [{"type": "promotion", "sources": ["lessons/a.md"], "target_tier": "sop"}]
        ks.land_sweep(date(2026, 6, 6), cands, repo_root=self.root, memory_dir=self.root / "x")
        with patch.object(ks, "REPO_ROOT", self.root), \
             patch.object(ks, "MEMORY_DIR", self.root / "x"), \
             patch.object(ks, "_invoke_ticket_file") as mock_file:
            rc = ks.file_sweep_ticket(date(2026, 6, 7))
        self.assertEqual(rc, 0)
        self.assertFalse(mock_file.called)


# ---------------------------------------------------------------------------
# Default-safe contract (T-0261).
# ---------------------------------------------------------------------------


class TestDefaultSafeGate(unittest.TestCase):
    def _args(self, **kw) -> object:
        import argparse
        ns = argparse.Namespace(
            file_sweep_ticket=False, land_sweep=False, dry_run=False,
            repo_root=None, force=False, candidates_file=None,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_bare_invocation_refused(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(ks._enforce_default_safe(self._args()), 64)

    def test_prod_and_safe_together_refused(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(
                ks._enforce_default_safe(
                    self._args(file_sweep_ticket=True, dry_run=True)
                ),
                64,
            )

    def test_prod_with_repo_root_refused(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(
                ks._enforce_default_safe(
                    self._args(file_sweep_ticket=True, repo_root=Path("/tmp/x"))
                ),
                64,
            )

    def test_file_sweep_ticket_alone_accepted(self) -> None:
        self.assertIsNone(
            ks._enforce_default_safe(self._args(file_sweep_ticket=True))
        )

    def test_land_sweep_alone_accepted(self) -> None:
        self.assertIsNone(ks._enforce_default_safe(self._args(land_sweep=True)))

    def test_two_prod_modes_refused(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(
                ks._enforce_default_safe(
                    self._args(file_sweep_ticket=True, land_sweep=True)
                ),
                64,
            )

    def test_dry_run_alone_accepted(self) -> None:
        self.assertIsNone(ks._enforce_default_safe(self._args(dry_run=True)))

    def test_repo_root_alone_accepted(self) -> None:
        self.assertIsNone(
            ks._enforce_default_safe(self._args(repo_root=Path("/tmp/x")))
        )


if __name__ == "__main__":
    unittest.main()
