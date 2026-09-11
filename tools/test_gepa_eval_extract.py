"""Unit tests for tools/gepa_eval_extract.py.

Run from repo root:
    python3 -m unittest tools.test_gepa_eval_extract -v

All fixtures are constructed in tempdirs by these tests. The live
tickets/ tree is never read here (see the T-2809 report for why: fixture
determinism, not corpus-content drift). A separate sanctioned wet run
against the real tickets/done/ corpus happens outside this suite.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import gepa_eval_extract as gee
from tools import ticket


def _write_ticket(
    path: Path,
    *,
    tid: str = "T-0001",
    requester: str = "orchestrator",
    target: str = "orchestrator",
    title: str = "Example ticket",
    labels: list[str] | None = None,
    log_body: str = "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox",
) -> None:
    """Write a well-formed done-ticket file for tests."""
    fm_lines = [
        "---",
        f"id: {tid}",
        "created: 2026-01-01T00:00:00Z",
        f"requester: {requester}",
        f"target: {target}",
        "state: done",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if labels:
        fm_lines.append(f"labels: [{', '.join(labels)}]")
    fm_lines.append("---")

    body_lines = [
        "",
        f"# {title}",
        "",
        "## Ask",
        "",
        "Example ask text.",
        "",
        "## Transition log",
        "",
        log_body,
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm_lines + body_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# split_transition_log_bullets
# ---------------------------------------------------------------------------


class TestSplitTransitionLogBullets(unittest.TestCase):
    def test_single_line_note(self) -> None:
        body = (
            "# Title\n\n"
            "## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [orchestrator] resolved; text here\n"
        )
        self.assertEqual(
            gee.split_transition_log_bullets(body),
            ["resolved; text here"],
        )

    def test_no_transition_log_header_returns_empty(self) -> None:
        body = "# Title\n\n## Ask\n\nsome text, no log section at all\n"
        self.assertEqual(gee.split_transition_log_bullets(body), [])

    def test_preserves_multiline_note_verbatim(self) -> None:
        body = (
            "## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [specialist] resolved @2fedf9c57; EVALUATED. The diagnosis transfers and we\n"
            "already adopted the core principle.\n"
            "\n"
            "SOURCE VERIFIED. Second paragraph continues here.\n"
        )
        expected = (
            "resolved @2fedf9c57; EVALUATED. The diagnosis transfers and we\n"
            "already adopted the core principle.\n"
            "\n"
            "SOURCE VERIFIED. Second paragraph continues here."
        )
        notes = gee.split_transition_log_bullets(body)
        self.assertEqual(notes, [expected])

    def test_stops_at_markdown_header(self) -> None:
        body = (
            "## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [claude] resolved at repo@abc1234 (round-2 APPROVE; round-1 REBOUND on X).\n"
            "\n"
            "## Resolution\n\n"
            "This is unrelated elaboration that must not leak into the bullet note.\n"
        )
        notes = gee.split_transition_log_bullets(body)
        self.assertEqual(len(notes), 1)
        self.assertEqual(
            notes[0],
            "resolved at repo@abc1234 (round-2 APPROVE; round-1 REBOUND on X).",
        )
        self.assertNotIn("unrelated elaboration", notes[0])

    def test_multiple_bullets_stay_in_source_order(self) -> None:
        body = (
            "## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox\n"
            "- 2026-01-01T00:01:00Z [orchestrator] claimed; moving to active\n"
            "- 2026-01-01T00:02:00Z [orchestrator] resolved; done here\n"
        )
        self.assertEqual(
            gee.split_transition_log_bullets(body),
            ["filed in inbox", "claimed; moving to active", "resolved; done here"],
        )

    def test_uses_last_transition_log_header_if_duplicated(self) -> None:
        body = (
            "## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [orchestrator] stale bullet, should not appear\n"
            "\n"
            "## Transition log\n\n"
            "- 2026-01-01T00:01:00Z [orchestrator] resolved; real bullet\n"
        )
        self.assertEqual(
            gee.split_transition_log_bullets(body),
            ["resolved; real bullet"],
        )


# ---------------------------------------------------------------------------
# extract_rebound_notes
# ---------------------------------------------------------------------------


class TestExtractReboundNotes(unittest.TestCase):
    def test_filters_only_matching(self) -> None:
        notes = ["filed in inbox", "resolved; 1/3 REBOUND consumed", "claimed; moving to active"]
        self.assertEqual(
            gee.extract_rebound_notes(notes),
            ["resolved; 1/3 REBOUND consumed"],
        )

    def test_case_sensitive_excludes_lowercase(self) -> None:
        notes = ["outcome-class vocab: one-rebound/multi-rebound taxonomy", "resolved; REBOUND consumed"]
        self.assertEqual(
            gee.extract_rebound_notes(notes),
            ["resolved; REBOUND consumed"],
        )


# ---------------------------------------------------------------------------
# extract_sha_annotations
# ---------------------------------------------------------------------------


class TestExtractShaAnnotations(unittest.TestCase):
    def test_finds_all_anchors_in_order(self) -> None:
        rebound_notes = [
            "resolved @2fedf9c57; REBOUND consumed, see also @3d2bf91 for context",
        ]
        self.assertEqual(
            gee.extract_sha_annotations(rebound_notes),
            ["@2fedf9c57", "@3d2bf91"],
        )

    def test_ignores_non_hex_at_mentions(self) -> None:
        rebound_notes = ["REBOUND raised by @someone, no real sha here"]
        self.assertEqual(gee.extract_sha_annotations(rebound_notes), [])

    def test_empty_when_none_present(self) -> None:
        self.assertEqual(gee.extract_sha_annotations(["REBOUND with no anchor"]), [])


# ---------------------------------------------------------------------------
# extract_resolved_note
# ---------------------------------------------------------------------------


class TestExtractResolvedNote(unittest.TestCase):
    def test_returns_last_resolved_bullet(self) -> None:
        notes = [
            "filed in inbox",
            "resolved; first resolution",
            "reopened; needs more work",
            "resolved; second resolution",
        ]
        self.assertEqual(gee.extract_resolved_note(notes), "resolved; second resolution")

    def test_none_when_absent(self) -> None:
        notes = ["filed in inbox", "claimed; moving to active", "blocked; waiting"]
        self.assertIsNone(gee.extract_resolved_note(notes))


# ---------------------------------------------------------------------------
# extract_approve_mentions
# ---------------------------------------------------------------------------


class TestExtractApproveMentions(unittest.TestCase):
    def test_counts_notes_with_uppercase_approve_only(self) -> None:
        notes = [
            "resolved; round-1 APPROVE from reviewer",
            "note; round-2 APPROVE confirmed",
            "note; the change was approved by the team",
        ]
        self.assertEqual(gee.extract_approve_mentions(notes), 2)

    def test_zero_when_absent(self) -> None:
        notes = ["filed in inbox", "resolved; done"]
        self.assertEqual(gee.extract_approve_mentions(notes), 0)


# ---------------------------------------------------------------------------
# is_refused_out_path
# ---------------------------------------------------------------------------


class TestIsRefusedOutPath(unittest.TestCase):
    def test_true_for_path_inside_repo_not_tmp(self) -> None:
        repo_root = Path("/home/claude/pantheon-config")
        out_path = repo_root / "tickets" / "eval.jsonl"
        self.assertTrue(gee.is_refused_out_path(out_path, repo_root))

    def test_false_for_tmp_path_nested_under_tmp_staged_repo_root(self) -> None:
        repo_root = Path("/tmp/pantheon-config-T-2809")
        out_path = repo_root / "scratch" / "eval.jsonl"
        self.assertFalse(gee.is_refused_out_path(out_path, repo_root))

    def test_false_for_unrelated_path_outside_repo(self) -> None:
        repo_root = Path("/home/claude/pantheon-config")
        out_path = Path("/home/claude/somewhere-else/eval.jsonl")
        self.assertFalse(gee.is_refused_out_path(out_path, repo_root))


# ---------------------------------------------------------------------------
# scan_ticket_file
# ---------------------------------------------------------------------------


class TestScanTicketFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.done_dir = self.root / "tickets" / "done"
        self.done_dir.mkdir(parents=True)

    def test_selects_ticket_with_rebound_mention(self) -> None:
        path = self.done_dir / "T-0001.md"
        _write_ticket(
            path,
            tid="T-0001",
            requester="recovery",
            target="orchestrator",
            title="Fix the thing",
            labels=["bug", "cli"],
            log_body=(
                "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox\n"
                "- 2026-01-01T00:01:00Z [orchestrator] resolved @abc1234; "
                "1/3 REBOUND consumed, fix landed"
            ),
        )
        record = gee.scan_ticket_file(path, self.root)
        self.assertIsNotNone(record)
        self.assertEqual(record["ticket_id"], "T-0001")
        self.assertEqual(record["title"], "Fix the thing")
        self.assertEqual(record["labels"], ["bug", "cli"])
        self.assertEqual(record["requester"], "recovery")
        self.assertEqual(record["target"], "orchestrator")
        self.assertEqual(
            record["rebound_notes"],
            ["resolved @abc1234; 1/3 REBOUND consumed, fix landed"],
        )
        self.assertEqual(record["sha_annotations"], ["@abc1234"])
        self.assertEqual(
            record["resolved_note"],
            "resolved @abc1234; 1/3 REBOUND consumed, fix landed",
        )
        self.assertEqual(record["approve_mentions"], 0)
        self.assertEqual(record["source_path"], "tickets/done/T-0001.md")

    def test_excludes_ticket_without_rebound_mention(self) -> None:
        path = self.done_dir / "T-0002.md"
        _write_ticket(
            path,
            tid="T-0002",
            log_body=(
                "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox\n"
                "- 2026-01-01T00:01:00Z [orchestrator] resolved; clean, no rebound"
            ),
        )
        self.assertIsNone(gee.scan_ticket_file(path, self.root))

    def test_tolerates_missing_frontmatter(self) -> None:
        path = self.done_dir / "README.md"
        path.write_text("# tickets/done/\n\nResolved tickets. Audit trail; do not modify.\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = gee.scan_ticket_file(path, self.root)
        self.assertIsNone(result)
        self.assertIn("README.md", err.getvalue())

    def test_tolerates_empty_file(self) -> None:
        path = self.done_dir / "T-0003.md"
        path.write_text("", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = gee.scan_ticket_file(path, self.root)
        self.assertIsNone(result)

    def test_tolerates_missing_required_field(self) -> None:
        path = self.done_dir / "T-0004.md"
        # Missing `id` (a REQUIRED_FRONT_MATTER field).
        path.write_text(
            "---\n"
            "created: 2026-01-01T00:00:00Z\n"
            "requester: orchestrator\n"
            "target: orchestrator\n"
            "state: done\n"
            "---\n\n"
            "# Title\n\n## Transition log\n\n"
            "- 2026-01-01T00:00:00Z [orchestrator] resolved; REBOUND happened\n",
            encoding="utf-8",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = gee.scan_ticket_file(path, self.root)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# scan_tickets_done
# ---------------------------------------------------------------------------


class TestScanTicketsDone(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.done_dir = self.root / "tickets" / "done"
        self.done_dir.mkdir(parents=True)

        # 5 REBOUND-matching tickets (T-0001..T-0005).
        for i in range(1, 6):
            tid = f"T-{i:04d}"
            _write_ticket(
                self.done_dir / f"{tid}.md",
                tid=tid,
                log_body=(
                    "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox\n"
                    f"- 2026-01-01T00:01:00Z [orchestrator] resolved; REBOUND #{i} consumed"
                ),
            )
        # 2 non-matching tickets.
        for i in range(6, 8):
            tid = f"T-{i:04d}"
            _write_ticket(
                self.done_dir / f"{tid}.md",
                tid=tid,
                log_body="- 2026-01-01T00:00:00Z [orchestrator] resolved; clean landing",
            )
        # 1 malformed file (no frontmatter).
        (self.done_dir / "README.md").write_text("# not a ticket\n", encoding="utf-8")

    def test_respects_limit_counting_only_matches(self) -> None:
        records = gee.scan_tickets_done(self.done_dir, self.root, limit=3)
        self.assertEqual(len(records), 3)
        self.assertEqual(
            [r["ticket_id"] for r in records],
            ["T-0001", "T-0002", "T-0003"],
        )

    def test_no_limit_returns_all_matches_only(self) -> None:
        records = gee.scan_tickets_done(self.done_dir, self.root, limit=None)
        self.assertEqual(
            [r["ticket_id"] for r in records],
            ["T-0001", "T-0002", "T-0003", "T-0004", "T-0005"],
        )


# ---------------------------------------------------------------------------
# main() — CLI end-to-end
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fake_repo_root = Path(self._tmp.name)
        self.fake_tickets_root = self.fake_repo_root / "tickets"
        self.done_dir = self.fake_tickets_root / "done"
        self.done_dir.mkdir(parents=True)

        for i in range(1, 4):
            tid = f"T-{i:04d}"
            _write_ticket(
                self.done_dir / f"{tid}.md",
                tid=tid,
                log_body=(
                    "- 2026-01-01T00:00:00Z [orchestrator] filed in inbox\n"
                    f"- 2026-01-01T00:01:00Z [orchestrator] resolved; REBOUND #{i} consumed"
                ),
            )

        self._out_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._out_dir.cleanup)

    def test_end_to_end_writes_jsonl_and_reports_count(self) -> None:
        out_path = Path(self._out_dir.name) / "out.jsonl"
        with patch.object(ticket, "TICKETS_ROOT", self.fake_tickets_root):
            rc = gee.main(["--out", str(out_path), "--limit", "2"])
        self.assertEqual(rc, 0)
        lines = out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            record = json.loads(line)
            self.assertIn("ticket_id", record)
            self.assertIn("rebound_notes", record)
            self.assertIn("approve_mentions", record)

    def test_refuses_out_path_inside_repo(self) -> None:
        # fake_repo_root is NOT under /tmp in this assertion's premise being
        # false is fine — it IS under /tmp here (tempfile), so to exercise
        # the refusal we target a path inside fake_repo_root that mimics a
        # non-/tmp repo by asserting against the pure function directly is
        # covered elsewhere; here we confirm main() wires is_refused_out_path
        # correctly using a path clearly inside the repo tree itself.
        out_path = self.fake_tickets_root / "eval.jsonl"
        with patch.object(ticket, "TICKETS_ROOT", self.fake_tickets_root), patch.object(
            gee, "is_refused_out_path", return_value=True
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = gee.main(["--out", str(out_path)])
        self.assertNotEqual(rc, 0)
        self.assertFalse(out_path.exists())
        self.assertIn("REFUSED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
