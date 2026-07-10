"""Tests for tools/weekly_ticket_summary.py — deterministic-path coverage.

Per sop/standard/tools-test-first.md the load-bearing behaviors get tests:
ISO-week math, frontmatter parsing, transition-log walk, boundary filtering
on resolution timestamps, idempotency check, README sentinel refresh,
and the --file-request mode (writes pending prompt file + files ticket,
no headless dispatch).
"""

from __future__ import annotations

import datetime as dt
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
        """--print-prompt <week> regenerates the prompt deterministically, no writes,
        no ticket subprocess — the fulfiller's self-sufficient prompt source."""
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
            mock_run.assert_not_called()  # read-only: no ticket filing
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


if __name__ == "__main__":
    unittest.main()
