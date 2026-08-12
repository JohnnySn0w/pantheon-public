"""Per-emitter escape-on-emit + no-churn tests for the T-1936 table emitters.

Each test calls the REAL emitter function with a pipe-carrying cell and asserts
the pipe is escaped in the emitted row — never re-implementing the row shape.
That is the anti-false-red discipline
(lessons/2026-07-22-false-red-verifier-must-call-prod-parser.md): a test that
constructs the expected row itself would pass even if the emitter bypassed the
shared escaper. These call prod.

Each test also asserts byte-identity to the historical hand-written literal on
pipe-free input — the no-churn guarantee that regenerating a live table (none of
which carry a pipe today) is a zero-diff.

Fixture-string note: the HERMES trigger byte sequence is never written in
contiguous form in this file.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import s3star_audit_runner as s3
from tools import ticket as tk
from tools import watch_runner as wr


class TestTicketReadmeRowEmitter(unittest.TestCase):
    """ticket.py _partition_done_rows — the tickets/README Current-state table."""

    def _ticket(self, tid: str, title: str, state: str = "done"):
        return tk.Ticket(
            path=Path("tickets") / state / f"{tid}.md",
            fields={"id": tid, "state": state, "requester": "atlas",
                    "target": "orchestrator", "created": "2026-07-23T00:00:00Z"},
            body="",
            title=title,
        )

    def test_pipe_in_title_is_escaped_on_emit(self) -> None:
        full, _ = tk._partition_done_rows([self._ticket("T-0001", "fix a | b parser")])
        self.assertEqual(len(full), 1)
        self.assertIn(r"fix a \| b parser", full[0])
        # The escaped pipe must not read as a bare cell break.
        self.assertNotIn("a | b parser", full[0])

    def test_pipe_free_row_is_byte_identical_to_old_literal(self) -> None:
        full, _ = tk._partition_done_rows([self._ticket("T-0001", "a plain title")])
        expected = "| T-0001 | done | a plain title | atlas → orchestrator | 2026-07-23 |"
        self.assertEqual(full[0], expected)


class TestWatchRunnerDayIndexEmitter(unittest.TestCase):
    """watch_runner.append_day_index_entry — the per-day watch index table."""

    def test_pipe_in_summary_is_escaped_on_emit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wr.append_day_index_entry(d, "2026-07-23-x", "ok", "found A | B in logs")
            text = (d / "README.md").read_text(encoding="utf-8")
            self.assertIn(r"found A \| B in logs", text)
            self.assertNotIn("A | B in logs", text)

    def test_pipe_free_entry_is_byte_identical_to_old_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wr.append_day_index_entry(d, "2026-07-23-x", "ok", "a plain summary")
            text = (d / "README.md").read_text(encoding="utf-8")
            self.assertIn(
                "| [2026-07-23-x](2026-07-23-x.md) | ok | a plain summary |\n", text
            )


class TestS3starManifestEmitter(unittest.TestCase):
    """s3star_audit_runner._write_evidence_bundle — the evidence MANIFEST table."""

    def _cmd(self, label: str):
        return s3._SnapshotCommand(label=label, argv=["true"], slug="snap")

    def test_pipe_in_label_is_escaped_on_emit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ev = Path(td) / "evidence"
            s3._write_evidence_bundle(
                ev, [(self._cmd("weekly | narrow"), "body")],
                generated_at="2026-07-23T00:00:00Z",
            )
            manifest = (ev / "MANIFEST.md").read_text(encoding="utf-8")
            self.assertIn(r"weekly \| narrow", manifest)
            self.assertNotIn("| weekly | narrow |", manifest)

    def test_pipe_free_label_row_is_byte_identical_to_old_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ev = Path(td) / "evidence"
            s3._write_evidence_bundle(
                ev, [(self._cmd("Weekly narrow"), "body")],
                generated_at="2026-07-23T00:00:00Z",
            )
            manifest = (ev / "MANIFEST.md").read_text(encoding="utf-8")
            # digest is content-derived; assert the stable cells verbatim.
            self.assertIn("| `snap.txt` | Weekly narrow | OK | `", manifest)


class TestWeeklyReadmeEmitter(unittest.TestCase):
    """weekly_ticket_summary.refresh_readme_index — the reports/README index table."""

    _README = (
        "# reports/\n\n## Reports\n\n"
        "<!-- AUTOGEN-START spine-index -->\n(empty)\n"
        "<!-- AUTOGEN-END spine-index -->\n"
    )

    def _run(self, report_body: str) -> str:
        from unittest import mock

        from tools import weekly_ticket_summary as wts

        with tempfile.TemporaryDirectory() as td:
            reports = Path(td)
            readme = reports / "README.md"
            readme.write_text(self._README)
            (reports / "2026-W30.md").write_text(report_body)
            with mock.patch.object(wts, "REPORTS_DIR", reports), \
                 mock.patch.object(wts, "README_PATH", readme):
                wts.refresh_readme_index()
            return readme.read_text()

    def test_pipe_in_heading_is_escaped_on_emit(self) -> None:
        text = self._run("---\nstatus: GENERATED\n---\n\n# Week A | B\n\nBody.\n")
        self.assertIn(r"Week A \| B", text)
        self.assertNotIn("| Week A | B |", text)

    def test_pipe_free_row_is_byte_identical_to_old_literal(self) -> None:
        text = self._run("---\nstatus: GENERATED\n---\n\n# Week of 2026-07-20\n\nBody.\n")
        self.assertIn("| `2026-W30.md` | Week of 2026-07-20 | GENERATED |", text)


if __name__ == "__main__":
    unittest.main()
