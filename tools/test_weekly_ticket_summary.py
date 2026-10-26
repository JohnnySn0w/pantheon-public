"""Tests for tools/weekly_ticket_summary.py — deterministic-path coverage.

Per sop/standard/tools-test-first.md the load-bearing behaviors get tests:
ISO-week math, frontmatter parsing, transition-log walk, boundary filtering
on resolution timestamps, idempotency check, README sentinel refresh,
and the --file-request mode (writes pending prompt file + files ticket,
no headless dispatch).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Import the module under test as a package member. (Previously this used a
# sys.path.insert(tools/) path-hack, but that polluted sys.path process-wide,
# letting other modules' run-as-script bare imports load a SECOND module
# identity — which broke patch targets elsewhere in the suite. T-0856.)
import tools.weekly_ticket_summary as wts


class TestIsoWeekHelpers(unittest.TestCase):
    def test_iso_week_range_known_week(self):
        # 2026-W21 = Monday 2026-05-18 → Sunday 2026-05-24
        mon, sun = wts.iso_week_range(2026, 21)
        self.assertEqual(mon, dt.date(2026, 5, 18))
        self.assertEqual(sun, dt.date(2026, 5, 24))

    def test_iso_week_range_first_week_of_year(self):
        # ISO 2026 week 1 starts Monday 2025-12-29 (per ISO 8601)
        mon, sun = wts.iso_week_range(2026, 1)
        self.assertEqual(mon, dt.date(2025, 12, 29))
        self.assertEqual(sun, dt.date(2026, 1, 4))

    def test_parse_week_arg_valid_forms(self):
        self.assertEqual(wts.parse_week_arg("2026-W21"), (2026, 21))
        self.assertEqual(wts.parse_week_arg("2026-w21"), (2026, 21))
        self.assertEqual(wts.parse_week_arg("2026-W5"), (2026, 5))

    def test_parse_week_arg_invalid(self):
        import argparse
        for bad in ("2026W21", "26-W21", "2026-21", "2026-W00", "2026-W54"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=f"should reject {bad}"):
                wts.parse_week_arg(bad)

    def test_prior_iso_week_friday(self):
        # Friday 2026-05-29: prior ISO week is 2026-W21 (May 18-24)
        today = dt.date(2026, 5, 29)
        self.assertEqual(wts.prior_iso_week(today), (2026, 21))

    def test_prior_iso_week_monday(self):
        # Monday 2026-05-25 (start of W22): prior week is W21
        today = dt.date(2026, 5, 25)
        self.assertEqual(wts.prior_iso_week(today), (2026, 21))

    def test_prior_iso_week_sunday(self):
        # Sunday 2026-05-24 (last day of W21): prior week is W20
        today = dt.date(2026, 5, 24)
        self.assertEqual(wts.prior_iso_week(today), (2026, 20))

    def test_week_label_format(self):
        self.assertEqual(wts.week_label(2026, 21), "2026-W21")
        self.assertEqual(wts.week_label(2026, 5), "2026-W05")


class TestParseTicketFile(unittest.TestCase):
    def _write_ticket(self, tmp: Path, name: str, body: str) -> Path:
        p = tmp / name
        p.write_text(body)
        return p

    def test_parses_complete_ticket(self):
        body = """---
id: T-0123
created: 2026-05-18T10:00:00Z
requester: operator
target: recovery
state: done
labels: [feat, infra]
intent: action
parent_ticket: T-0100
---

# Sample ticket title

## Ask

Some ask.

## Transition log

- 2026-05-18T10:00:00Z [operator] filed in inbox
- 2026-05-20T14:00:00Z [recovery] claimed; moving to active
- 2026-05-22T09:30:00Z [recovery] resolved — fixed at commit abc1234def
"""
        with tempfile.TemporaryDirectory() as tmpd:
            p = self._write_ticket(Path(tmpd), "T-0123.md", body)
            rec = wts.parse_ticket_file(p)
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec.id, "T-0123")
        self.assertEqual(rec.title, "Sample ticket title")
        self.assertEqual(rec.target, "recovery")
        self.assertEqual(rec.requester, "operator")
        self.assertEqual(rec.labels, ["feat", "infra"])
        self.assertEqual(rec.intent, "action")
        self.assertEqual(rec.parent_ticket, "T-0100")
        self.assertEqual(rec.resolve_action, "resolved")
        self.assertEqual(rec.resolve_actor, "recovery")
        assert rec.resolved_at is not None
        self.assertEqual(rec.resolved_at, dt.datetime(2026, 5, 22, 9, 30, tzinfo=dt.timezone.utc))
        self.assertIn("abc1234def", rec.commit_refs)

    def test_ticket_without_resolve_entry_returns_none(self):
        body = """---
id: T-0999
target: recovery
state: inbox
---

# Inbox ticket

## Transition log

- 2026-05-18T10:00:00Z [operator] filed in inbox
"""
        with tempfile.TemporaryDirectory() as tmpd:
            p = self._write_ticket(Path(tmpd), "T-0999.md", body)
            rec = wts.parse_ticket_file(p)
        self.assertIsNone(rec)


class TestWalkDoneTickets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.done = Path(self.tmp.name) / "done"
        self.done.mkdir()
        # Three tickets: one before window, one in window, one after window.
        for tid, resolve_ts in [
            ("T-0001", "2026-05-17T23:00:00Z"),  # before
            ("T-0002", "2026-05-19T12:00:00Z"),  # in
            ("T-0003", "2026-05-25T00:30:00Z"),  # after (week ends Sun 24th)
        ]:
            (self.done / f"{tid}.md").write_text(f"""---
id: {tid}
target: recovery
requester: operator
state: done
labels: []
---

# {tid} title

## Transition log

- {resolve_ts} [recovery] resolved
""")

    def tearDown(self):
        self.tmp.cleanup()

    def test_walks_only_in_window(self):
        with mock.patch.object(wts, "DONE_DIR", self.done):
            recs = wts.walk_done_tickets(dt.date(2026, 5, 18), dt.date(2026, 5, 24))
        self.assertEqual([r.id for r in recs], ["T-0002"])


class TestRefreshReadmeIndex(unittest.TestCase):
    def test_writes_when_changed(self):
        with tempfile.TemporaryDirectory() as tmpd:
            reports = Path(tmpd)
            readme = reports / "README.md"
            readme.write_text(
                "# reports/\n\n## Reports\n\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "(empty)\n"
                "<!-- AUTOGEN-END spine-index -->\n"
            )
            (reports / "2026-W21.md").write_text(
                "---\nstatus: GENERATED\n---\n\n# Week of 2026-05-18\n\nBody.\n"
            )
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme):
                changed = wts.refresh_readme_index()
            self.assertTrue(changed)
            new = readme.read_text()
        self.assertIn("`2026-W21.md`", new)
        self.assertIn("Week of 2026-05-18", new)
        self.assertIn("GENERATED", new)

    def test_noop_when_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpd:
            reports = Path(tmpd)
            readme = reports / "README.md"
            (reports / "2026-W21.md").write_text(
                "---\nstatus: GENERATED\n---\n\n# Week of 2026-05-18\n\nBody.\n"
            )
            initial = (
                "# reports/\n\n## Reports\n\n"
                "<!-- AUTOGEN-START spine-index -->\n"
                "| `2026-W21.md` | Week of 2026-05-18 | GENERATED |\n"
                "<!-- AUTOGEN-END spine-index -->\n"
            )
            readme.write_text(initial)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme):
                changed = wts.refresh_readme_index()
            self.assertFalse(changed)
            self.assertEqual(readme.read_text(), initial)


class TestBuildInputBundle(unittest.TestCase):
    def test_includes_count_and_blocks(self):
        rec = wts.TicketRecord(
            id="T-0123",
            title="Sample",
            target="recovery",
            requester="operator",
            labels=["feat"],
            resolved_at=dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc),
            resolve_actor="recovery",
            resolve_action="resolved",
            transition_log="- 2026-05-18T10:00:00Z [operator] filed",
            commit_refs=["abc1234"],
        )
        bundle = wts.build_input_bundle([rec], dt.date(2026, 5, 18), dt.date(2026, 5, 24))
        self.assertIn("Total tickets resolved: 1", bundle)
        self.assertIn("T-0123", bundle)
        self.assertIn("Sample", bundle)
        self.assertIn("abc1234", bundle)


class TestFinalize(unittest.TestCase):
    """Tests for the --finalize mode (in-session fulfiller commits the report)."""

    def _make_env_with_report(self, tmp: Path, label: str = "2026-W23") -> tuple[Path, Path, Path]:
        """Set up reports/ dir with a written report + README sentinel."""
        reports = tmp / "reports"
        reports.mkdir()
        readme = reports / "README.md"
        readme.write_text(
            "# reports/\n\n## Reports\n\n"
            "<!-- AUTOGEN-START spine-index -->\n"
            "(empty)\n"
            "<!-- AUTOGEN-END spine-index -->\n"
        )
        report = reports / f"{label}.md"
        report.write_text(
            f"---\nweek: {label}\nstatus: GENERATED\n---\n\n# Week of 2026-06-02\n\nBody.\n"
        )
        return reports, readme, report

    def test_finalize_refreshes_index_and_calls_commit(self):
        """--finalize refreshes the README index and calls commit_and_push."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, readme, report = self._make_env_with_report(tmp)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme), \
                 mock.patch.object(wts, "commit_and_push") as mock_commit:
                rc = wts.main(["--finalize", "2026-W23"])
            self.assertEqual(rc, 0)
            mock_commit.assert_called_once()
            call_args = mock_commit.call_args
            paths_arg = call_args[0][0]
            # report must be in the commit list
            self.assertIn(report, paths_arg)
            # README must be in the commit list (index was refreshed — it changed)
            self.assertIn(readme, paths_arg)
            # push=True by default (no --no-push); may be positional or keyword
            push_val = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("push", True)
            self.assertEqual(push_val, True)

    def test_finalize_no_push(self):
        """--finalize --no-push calls commit_and_push with push=False."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, readme, report = self._make_env_with_report(tmp)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme), \
                 mock.patch.object(wts, "commit_and_push") as mock_commit:
                rc = wts.main(["--finalize", "2026-W23", "--no-push"])
            self.assertEqual(rc, 0)
            mock_commit.assert_called_once()
            call_args = mock_commit.call_args
            push_val = call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("push", True)
            self.assertEqual(push_val, False)

    def test_finalize_missing_report_errors(self):
        """--finalize exits non-zero when the report file does not exist."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports = tmp / "reports"
            reports.mkdir()
            # No report written — finalize must fail.
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "commit_and_push") as mock_commit:
                rc = wts.main(["--finalize", "2026-W23"])
            self.assertNotEqual(rc, 0, "--finalize must return non-zero when report is missing")
            mock_commit.assert_not_called()

    def test_finalize_dry_run(self):
        """--finalize --dry-run prints intent but does not call commit_and_push."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, readme, report = self._make_env_with_report(tmp)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme), \
                 mock.patch.object(wts, "commit_and_push") as mock_commit:
                rc = wts.main(["--finalize", "2026-W23", "--dry-run"])
            self.assertEqual(rc, 0)
            mock_commit.assert_not_called()

    def test_finalize_no_subprocess_or_ticket(self):
        """--finalize must never fire subprocess.run (no ticket filing, no claude)."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, readme, report = self._make_env_with_report(tmp)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme), \
                 mock.patch.object(wts, "commit_and_push"), \
                 mock.patch("subprocess.run") as mock_run:
                wts.main(["--finalize", "2026-W23"])
            mock_run.assert_not_called()


class TestFileRequest(unittest.TestCase):
    """Tests for the --file-request mode (headless-free path)."""

    def setUp(self):
        # Pin the running account to the canonical filer (T-1191): --file-request
        # no-ops on any other account, so these tests (which assert filing DOES
        # happen) must not depend on which user runs the suite.
        self._getuser = mock.patch.object(
            wts.getpass, "getuser", return_value=wts.CANONICAL_FILER_ACCOUNT
        )
        self._getuser.start()
        self.addCleanup(self._getuser.stop)

    def _make_minimal_env(self, tmp: Path) -> tuple[Path, Path, Path]:
        """Set up minimal reports/, SOP, and exemplar files under tmp."""
        reports = tmp / "reports"
        reports.mkdir()
        sop = tmp / "sop" / "standard"
        sop.mkdir(parents=True)
        (sop / "weekly-ticket-summary-generation.md").write_text("SOP text.\n")
        exemplar = reports / "2026-W21.md"
        exemplar.write_text("---\nstatus: GENERATED\n---\n# Week of 2026-05-18\n")
        return reports, sop / "weekly-ticket-summary-generation.md", exemplar

    def test_file_request_writes_no_orphan_pending_file(self):
        """--file-request must NOT write reports/.pending/ (T-1191 follow-up: the
        prompt was an orphan file that never crossed accounts; it is regenerated
        on the fulfiller's side via --print-prompt instead)."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, sop_path, exemplar_path = self._make_minimal_env(tmp)
            with mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar_path), \
                 mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="filed T-9001\n", stderr="")
                (tmp / "done").mkdir()
                rc = wts.main(["--file-request", "--week", "2026-W23"])
            self.assertEqual(rc, 0)
            self.assertFalse((reports / ".pending").exists(), "no orphan .pending/ should be created")

    def test_print_prompt_regenerates_to_stdout(self):
        """--print-prompt <week> regenerates the prompt deterministically, writes
        nothing, and files NO ticket — the fulfiller's self-sufficient prompt source.
        It MAY do read-only git reads (the T-2939 scale-metrics block)."""
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, sop_path, exemplar_path = self._make_minimal_env(tmp)
            (tmp / "done").mkdir()
            buf = io.StringIO()
            with mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar_path), \
                 mock.patch("subprocess.run") as mock_run, \
                 redirect_stdout(buf):
                rc = wts.main(["--print-prompt", "2026-W23"])
            self.assertEqual(rc, 0)
            # print-prompt may do read-only git reads (scale metrics) but must file
            # NO ticket — assert the real invariant, not "no subprocess at all".
            for c in mock_run.call_args_list:
                cmd = c.args[0] if c.args else ""
                cmd_str = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
                self.assertNotIn("ticket.py", cmd_str, "print-prompt must not file a ticket")
            out = buf.getvalue()
            self.assertIn("SOP text.", out)       # prompt embeds the SOP
            self.assertIn("2026-W23", out)        # and the target week
            self.assertFalse((reports / ".pending").exists())  # and writes nothing

    def test_files_ticket_subprocess(self):
        """--file-request calls ticket.py file via subprocess with required args."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, sop_path, exemplar_path = self._make_minimal_env(tmp)
            (tmp / "done").mkdir()
            with mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar_path), \
                 mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="filed T-9002\n", stderr="")
                rc = wts.main(["--file-request", "--week", "2026-W23"])
            self.assertEqual(rc, 0)
            self.assertTrue(mock_run.called, "subprocess.run must be called to file the ticket")
            call_args = mock_run.call_args
            argv = call_args[0][0]
            # Must invoke ticket.py with 'file' subcommand.
            joined = " ".join(str(a) for a in argv)
            self.assertIn("ticket.py", joined)
            self.assertIn("file", argv)
            self.assertIn("--requester", argv)
            self.assertIn("orchestrator", argv)
            self.assertIn("--target", argv)
            self.assertIn("--title", argv)
            self.assertIn("--labels", argv)
            self.assertIn("--ask", argv)
            # --intent is a target=operator-specific surface; ticket.py rejects it
            # for target=orchestrator (T-0465/T-0852). The generation-request ticket is
            # target=orchestrator, so the argv must NOT pass --intent (regression guard:
            # passing it failed the weekly service run — prompt staged, ticket
            # never filed).
            self.assertNotIn("--intent", argv)
            # The --ask must mention the pending prompt path, the report path,
            # and --finalize (the fulfillment step); must NOT mention --no-commit.
            ask_idx = list(argv).index("--ask")
            ask_text = argv[ask_idx + 1]
            self.assertIn("--print-prompt", ask_text)  # regenerate, not read an orphan file
            self.assertNotIn(".pending", ask_text)
            self.assertIn("reports/2026-W23.md", ask_text)
            self.assertIn("--finalize", ask_text)
            self.assertNotIn("--no-commit", ask_text)

    def test_does_not_call_claude_p(self):
        """--file-request must never invoke a headless claude -p subprocess."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, sop_path, exemplar_path = self._make_minimal_env(tmp)
            (tmp / "done").mkdir()
            with mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar_path), \
                 mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="filed T-9003\n", stderr="")
                wts.main(["--file-request", "--week", "2026-W23"])
            for call in mock_run.call_args_list:
                argv = [str(a) for a in call[0][0]]
                # Intent: no headless `claude -p` subprocess. Match the claude
                # binary / headless flags, not the bare "claude" substring — the
                # latter false-trips on the /home/claude/ home path (T-0879).
                self.assertNotEqual(Path(argv[0]).name, "claude",
                    f"--file-request must not spawn the claude binary; got argv: {argv}")
                joined = " ".join(argv)
                for headless in ("claude -p", "claude --print"):
                    self.assertNotIn(headless, joined,
                        f"--file-request must not invoke headless {headless!r}; got: {joined}")

    def test_dry_run_no_side_effects(self):
        """--file-request --dry-run prints intent but writes nothing and calls no subprocess."""
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports, sop_path, exemplar_path = self._make_minimal_env(tmp)
            (tmp / "done").mkdir()
            with mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar_path), \
                 mock.patch("subprocess.run") as mock_run:
                rc = wts.main(["--file-request", "--dry-run", "--week", "2026-W23"])
            self.assertEqual(rc, 0)
            mock_run.assert_not_called()
            pending_dir = reports / ".pending"
            self.assertFalse(pending_dir.exists(), "dry-run must not create .pending/")


class TestCanonicalFilerGuard(unittest.TestCase):
    """T-1191 dedup: --file-request files only on the canonical account."""

    def _run_file_request(self, account: str, *, dry_run: bool = False):
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = Path(tmpd)
            reports = tmp / "reports"
            reports.mkdir()
            sop = tmp / "sop" / "standard"
            sop.mkdir(parents=True)
            sop_path = sop / "weekly-ticket-summary-generation.md"
            sop_path.write_text("SOP text.\n")
            exemplar = reports / "2026-W21.md"
            exemplar.write_text("---\nstatus: GENERATED\n---\n# Week\n")
            (tmp / "done").mkdir()
            argv = ["--file-request", "--week", "2026-W23"]
            if dry_run:
                argv.append("--dry-run")
            with mock.patch.object(wts.getpass, "getuser", return_value=account), \
                 mock.patch.object(wts, "DONE_DIR", tmp / "done"), \
                 mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "SOP_PATH", sop_path), \
                 mock.patch.object(wts, "EXEMPLAR_PATH", exemplar), \
                 mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0, stdout="filed T-9009\n", stderr="")
                rc = wts.main(argv)
                # Capture existence INSIDE the context — the tmpdir is deleted on exit.
                pending_written = (reports / ".pending").exists()
            return rc, mock_run, pending_written

    def test_non_canonical_account_noops(self):
        """A stray --file-request on a non-canonical account files nothing."""
        rc, mock_run, pending_written = self._run_file_request("claude")  # Orchestrator's account
        self.assertEqual(rc, 0)  # clean no-op, not an error
        mock_run.assert_not_called()  # no ticket.py filing subprocess
        self.assertFalse(pending_written, "non-canonical run must not write .pending/")

    def test_canonical_account_files(self):
        """The canonical account proceeds to file the ticket (no orphan .pending)."""
        rc, mock_run, pending_written = self._run_file_request(wts.CANONICAL_FILER_ACCOUNT)
        self.assertEqual(rc, 0)
        self.assertTrue(mock_run.called, "canonical account must file the ticket")
        self.assertFalse(pending_written, "no orphan .pending/ is written (regenerated via --print-prompt)")

    def test_dry_run_bypasses_guard_on_any_account(self):
        """--dry-run previews on any account (writes nothing regardless)."""
        rc, mock_run, pending_written = self._run_file_request("claude", dry_run=True)
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        self.assertFalse(pending_written)


class TestScaleMetrics(unittest.TestCase):
    """T-2939 scale-metrics block (the operator req 2026-08-12): exact, verbatim, never breaks."""

    def test_build_prompt_includes_scale_metrics_verbatim(self):
        prompt = wts.build_prompt(
            sop_text="SOP", exemplar_text="EX", bundle="B",
            target_path_rel="reports/2026-W23.md", week_label_str="2026-W23",
            monday=dt.date(2026, 6, 1), sunday=dt.date(2026, 6, 7), ticket_count=3,
            scale_metrics="### Pantheon at scale\n\n- SENTINEL 1,234 commits\n",
        )
        self.assertIn("SENTINEL 1,234 commits", prompt)          # embedded verbatim
        self.assertIn("reproduce VERBATIM", prompt)              # with the instruction
        self.assertIn("do NOT recompute", prompt)

    def test_build_prompt_scale_metrics_defaults_empty(self):
        # Back-compat: the param is optional; omitting it must not crash.
        prompt = wts.build_prompt(
            sop_text="S", exemplar_text="E", bundle="B", target_path_rel="r",
            week_label_str="2026-W23", monday=dt.date(2026, 6, 1),
            sunday=dt.date(2026, 6, 7), ticket_count=0,
        )
        self.assertIn("PRECOMPUTED SCALE METRICS", prompt)

    def test_compute_scale_metrics_degrades_gracefully(self):
        # A git/FS failure must NEVER break the weekly report — degrade to a comment.
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            block = wts.compute_scale_metrics()
        self.assertTrue(block.strip().startswith("<!--"), block)
        self.assertIn("unavailable", block)

    def test_compute_scale_metrics_timeout_degrades_to_documented_comment(self):
        # The fallback the real-repo smoke tolerates under load is exactly this one:
        # a git TimeoutExpired lands in the documented comment and names the timeout.
        exc = subprocess.TimeoutExpired(["git", "rev-list", "--count", "HEAD"], 120)
        with mock.patch("subprocess.run", side_effect=exc):
            block = wts.compute_scale_metrics()
        self.assertTrue(block.startswith("<!-- scale metrics unavailable: "), block)
        self.assertIn("timed out after 120 seconds", block)

    def test_compute_scale_metrics_exact_values_on_fixture_repo(self):
        """T-4345: pins the exact, comma-formatted path with REAL git, on a small
        fixture repo whose every number is known in advance (1,001 commits via
        fast-import, 1,200 md + 1,100 py lines, ticket ids up to T-1500). This is
        stronger than the real-repo smoke (which can only regex for a comma) and,
        being sub-second, it carries the formatting intent even when the smoke
        has to stand down under runner load."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            md = "alpha beta\n" * 1200   # 1,200 lines, 2,400 words, 13,200 bytes
            py = "x = 1\n" * 1100        # 1,100 lines, 3,300 words,  6,600 bytes
            stream = [
                "commit refs/heads/main",
                "committer T <t@example.invalid> 1700000000 +0000",
                "data 4", "init",
                f"M 644 inline big.md\ndata {len(md)}\n{md}",
                f"M 644 inline code.py\ndata {len(py)}\n{py}",
            ]
            for i in range(1000):  # 1,000 empty commits on top: 1,001 in all
                stream += ["commit refs/heads/main",
                           f"committer T <t@example.invalid> {1700000001 + i} +0000",
                           "data 1", "c"]
            no_git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            with mock.patch.dict(os.environ, no_git_env, clear=True):
                for args, stdin in ((["git", "init", "-q", "-b", "main"], None),
                                    (["git", "fast-import", "--quiet"], "\n".join(stream) + "\n"),
                                    (["git", "reset", "-q", "--hard", "main"], None)):
                    subprocess.run(args, cwd=repo, input=stdin, text=True, check=True,
                                   capture_output=True, timeout=120)
                # Tickets are counted from the filesystem, not git: left untracked
                # so they don't perturb the line/word/byte totals.
                for state, tid in (("done", "T-0007"), ("inbox", "T-1500"), ("active", "T-0042")):
                    (repo / "tickets" / state).mkdir(parents=True)
                    (repo / "tickets" / state / f"{tid}.md").write_text("t\n")
                block = wts.compute_scale_metrics(repo)
        self.assertTrue(block.startswith("### Pantheon at scale\n"), block)
        self.assertIn("- **1,500** tickets filed (1 done, 1 queued, 1 active) · **1,001** commits\n", block)
        self.assertIn("- **2,300** lines · **5,700** words · ~**0 MB** of text across **2** tracked files\n", block)
        self.assertIn("- Prose vs code runs near-even: **1,200** markdown lines (2,400 words) "
                      "to **1,100** Python lines\n", block)

    def test_compute_scale_metrics_real_repo_smoke(self):
        """Read-only integration against the real repo: exact, comma-formatted numbers.

        T-4345: on the loaded CI runner `git rev-list --count HEAD` (sub-second
        here: ~0.1s with a commit-graph, ~0.9s without) exceeded its 120s timeout
        under cgroup memory thrash, and the function took its documented
        degrade-to-a-comment path. That path is the contract, so a TIMEOUT is
        reported as a skip here, not a failure; the formatted path is pinned
        exactly by the fixture-repo test above. Any other fallback (an empty
        read, an exception from the code) still fails."""
        block = wts.compute_scale_metrics()
        if block.startswith("<!-- scale metrics unavailable: ") and "timed out after" in block:
            self.skipTest(f"git timed out under host load (documented fallback): {block.strip()}")
        self.assertIn("Pantheon at scale", block)
        self.assertIn("commits", block)
        self.assertIn("tickets filed", block)
        self.assertRegex(block, r"\d{1,3},\d{3}")  # thousands-separated numbers present


class TestScriptModeImports(unittest.TestCase):
    def test_adr_reeval_walk_importable_in_script_mode(self):
        """The systemd unit runs `python3 tools/weekly_ticket_summary.py`, so
        sys.path[0] is tools/ and a bare `from tools...` failed: the ADR re-eval
        walk never ran from the unit ("No module named 'tools'", 2026-08-03 to
        09-28). Emulate script mode in a clean interpreter, then hit the real
        call site.

        T-4230: the IMPORT is what this proves, but the call site then also ran the
        walk over the live repo (every ADR, plus the grand-program index lint over
        the ~4300-ticket tree): 21.5s alone, and it blew its 120s timeout at host
        load ~26. So the walk is now pointed at an empty tmp fixture. Nothing that
        is under test changes: the subprocess still runs the REAL
        `_import_run_walk()` under script-mode sys.path (the thing that broke) and
        the REAL `_adr_reeval_watch` call site; only the directories the walk
        scans are redirected, by wrapping the run_walk it returns. And the output
        is now also required NOT to be the degrade-to-a-comment path, which
        `_adr_reeval_watch` takes on any exception (that path is how the original
        defect stayed silent, and how a broken fixture would too)."""
        import os
        import subprocess
        import sys
        script = Path(wts.__file__).resolve()
        with tempfile.TemporaryDirectory(prefix="t4230-adr-") as tmp:
            (Path(tmp) / "adr").mkdir()
            (Path(tmp) / "tickets" / "done").mkdir(parents=True)
            code = (
                "import datetime as dt, pathlib, runpy, sys\n"
                f"sys.path[0] = {str(script.parent)!r}\n"
                f"m = runpy.run_path({str(script)!r})\n"
                "g = m['_adr_reeval_watch'].__globals__\n"
                "real_import = g['_import_run_walk']\n"
                "def redirected():\n"
                "    run_walk = real_import()  # the REAL script-mode import under test\n"
                "    return lambda **kw: run_walk(**{**kw,\n"
                f"        'adr_dir': pathlib.Path({tmp + '/adr'!r}),\n"
                f"        'tickets_root': pathlib.Path({tmp + '/tickets'!r})}})\n"
                "g['_import_run_walk'] = redirected\n"
                "print(m['_adr_reeval_watch']('', dt.date(2026, 9, 27)))\n"
            )
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, cwd="/", env=env,
                               timeout=120)  # a HANG bound only; the hermetic run is ~1s CPU
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("No module named 'tools'", r.stdout)
        self.assertNotIn("unavailable", r.stdout, "walk degraded to its failure comment")


if __name__ == "__main__":
    unittest.main()
