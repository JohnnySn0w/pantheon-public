"""Tests for tools.activity_digest."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Bootstrap: same sys.path trick as inbox_consumer.py.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.activity_digest import (
    compute_window,
    load_backlog_arcs,
    classify_arc,
    classify_project,
    gather_resolved_tickets,
    render_digest,
    send_telegram,
    write_ledger_entry,
    write_log,
    _strip_md,
    FIXED_OFFSETS,
)

from tools.ticket import Ticket, TICKETS_ROOT


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _make_ticket(
    ticket_id: str,
    labels: list[str] | None = None,
    resolve_line: str = "",
    resolve_ts: str = "2026-05-04T21:00:00Z",
    parent_ticket: str | None = None,
) -> Ticket:
    """Build a minimal Ticket object for testing (no file I/O)."""
    labels = labels or []
    labels_str = "[" + ", ".join(labels) + "]" if labels else "[]"
    parent_str = parent_ticket if parent_ticket else "null"
    resolve_body = resolve_line or f"resolved; did the work."
    body = (
        f"# Ticket title for {ticket_id}\n\n"
        f"## Ask\n\nDo the thing.\n\n"
        f"## Transition log\n\n"
        f"- 2026-05-04T10:00:00Z [orchestrator] filed in inbox\n"
        f"- {resolve_ts} [orchestrator] {resolve_body}\n"
    )
    fields = {
        "id": ticket_id,
        "created": "2026-05-04T10:00:00Z",
        "requester": "orchestrator",
        "target": "orchestrator",
        "state": "done",
        "parent_ticket": parent_ticket,
        "depends_on": [],
        "labels": labels,
        "stale_after": "7d",
    }
    return Ticket(path=Path(f"/fake/tickets/done/{ticket_id}.md"), fields=fields, body=body, title=f"Ticket title for {ticket_id}")


def _write_ticket_file(dir_path: Path, ticket_id: str, labels: list[str],
                       resolve_ts: str, resolve_line: str = "resolved; work done.") -> Path:
    """Write a real ticket file to disk for gather_resolved_tickets tests."""
    done_dir = dir_path / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    labels_str = "[" + ", ".join(labels) + "]" if labels else "[]"
    content = (
        "---\n"
        f"id: {ticket_id}\n"
        f"created: 2026-05-04T10:00:00Z\n"
        f"requester: orchestrator\n"
        f"target: orchestrator\n"
        f"state: done\n"
        f"parent_ticket: null\n"
        f"depends_on: []\n"
        f"labels: {labels_str}\n"
        f"stale_after: 7d\n"
        "---\n\n"
        f"# Title for {ticket_id}\n\n"
        "## Ask\n\nDo the thing.\n\n"
        "## Transition log\n\n"
        f"- 2026-05-04T10:00:00Z [orchestrator] filed in inbox\n"
        f"- {resolve_ts} [orchestrator] {resolve_line}\n"
    )
    path = done_dir / f"{ticket_id}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# TestWindowBoundary — 6 cases.
# ---------------------------------------------------------------------------


class TestWindowBoundary(unittest.TestCase):
    def _window_start_for(self, hour: int, minute: int, second: int = 0) -> datetime:
        fixed_now = datetime(2026, 5, 4, hour, minute, second, tzinfo=timezone.utc)
        with patch("tools.activity_digest.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            start, end = compute_window()
        return start

    def test_00_boundary(self):
        """00:42 → boundary 00:00."""
        start = self._window_start_for(0, 42)
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.minute, 0)
        self.assertEqual(start.second, 0)

    def test_05_boundary_exact(self):
        """05:00 exactly → boundary 05:00."""
        start = self._window_start_for(5, 0)
        self.assertEqual(start.hour, 5)
        self.assertEqual(start.minute, 0)

    def test_10_boundary(self):
        """10:59 → boundary 10:00."""
        start = self._window_start_for(10, 59)
        self.assertEqual(start.hour, 10)

    def test_15_boundary(self):
        """15:01 → boundary 15:00."""
        start = self._window_start_for(15, 1)
        self.assertEqual(start.hour, 15)

    def test_20_boundary(self):
        """20:33 → boundary 20:00."""
        start = self._window_start_for(20, 33)
        self.assertEqual(start.hour, 20)

    def test_non_aligned_13_42(self):
        """13:42 → boundary 10:00 (floor(13/5)*5 = 10)."""
        start = self._window_start_for(13, 42)
        self.assertEqual(start.hour, 10)

    def test_window_end_equals_now(self):
        """window_end == now (the raw datetime.now result)."""
        fixed_now = datetime(2026, 5, 4, 13, 42, 7, tzinfo=timezone.utc)
        with patch("tools.activity_digest.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            start, end = compute_window()
        self.assertEqual(end, fixed_now)

    def test_window_is_utc(self):
        """window_start is timezone-aware UTC."""
        fixed_now = datetime(2026, 5, 4, 7, 30, tzinfo=timezone.utc)
        with patch("tools.activity_digest.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            start, _ = compute_window()
        self.assertIsNotNone(start.tzinfo)


# ---------------------------------------------------------------------------
# TestArcDetection — classify_arc().
# ---------------------------------------------------------------------------


class TestArcDetection(unittest.TestCase):
    def test_label_b10_detected(self):
        """Ticket with label 'b10' → arc 'B10'."""
        t = _make_ticket("T-0063", labels=["b10", "activity-digest", "implementation"])
        self.assertEqual(classify_arc(t), "B10")

    def test_label_b9_detected(self):
        """Ticket with label 'b9' → arc 'B9'."""
        t = _make_ticket("T-0052", labels=["b9", "autonomy", "build"])
        self.assertEqual(classify_arc(t), "B9")

    def test_no_arc_label_returns_none(self):
        """Ticket with no b<N> label → None."""
        t = _make_ticket("T-0001", labels=["stub", "backlog-arc"])
        self.assertIsNone(classify_arc(t))

    def test_empty_labels_returns_none(self):
        """Ticket with empty labels → None."""
        t = _make_ticket("T-0002", labels=[])
        self.assertIsNone(classify_arc(t))

    def test_arc_label_case_insensitive(self):
        """Label 'B9' (uppercase) also detected."""
        t = _make_ticket("T-0099", labels=["B9", "foo"])
        self.assertEqual(classify_arc(t), "B9")

    def test_multiple_arc_labels_returns_first(self):
        """If somehow two b<N> labels present, returns the first match."""
        t = _make_ticket("T-0099", labels=["b9", "b10"])
        result = classify_arc(t)
        self.assertIn(result, ("B9", "B10"))

    def test_partial_match_not_detected(self):
        """Label 'b9-smoke' should not match (not a bare b<N>)."""
        t = _make_ticket("T-0055", labels=["b9-smoke", "test-fixture"])
        self.assertIsNone(classify_arc(t))


# ---------------------------------------------------------------------------
# TestProjectClassification — classify_project().
# ---------------------------------------------------------------------------


class TestProjectClassification(unittest.TestCase):
    def test_pantheon_bridge_in_resolve(self):
        """Resolve line references pantheon-bridge → ['pantheon-bridge']."""
        t = _make_ticket(
            "T-0051",
            resolve_line="resolved; bridge /health endpoint landed at pantheon-bridge@abc1234.",
        )
        result = classify_project(t)
        self.assertEqual(result, ["pantheon-bridge"])

    def test_pantheon_config_only(self):
        """Resolve line with no repo mention → ['pantheon-config']."""
        t = _make_ticket(
            "T-0052",
            resolve_line="resolved; inbox_consumer.py landed at commit 27ac57f.",
        )
        result = classify_project(t)
        self.assertEqual(result, ["pantheon-config"])

    def test_both_repos_in_resolve(self):
        """Resolve line mentions both repos → both appear in result."""
        t = _make_ticket(
            "T-0099",
            resolve_line=(
                "resolved; changes landed at pantheon-config@abc and pantheon-bridge@def."
            ),
        )
        result = classify_project(t)
        self.assertIn("pantheon-config", result)
        self.assertIn("pantheon-bridge", result)

    def test_no_resolve_line_defaults_to_config(self):
        """Ticket with no resolve transition → ['pantheon-config']."""
        fields = {
            "id": "T-0099",
            "created": "2026-05-04T10:00:00Z",
            "requester": "orchestrator",
            "target": "orchestrator",
            "state": "done",
            "parent_ticket": None,
            "depends_on": [],
            "labels": [],
            "stale_after": "7d",
        }
        body = "# Title\n\n## Transition log\n\n- 2026-05-04T10:00:00Z [ho] filed in inbox\n"
        t = Ticket(path=Path("/fake/T-0099.md"), fields=fields, body=body, title="Title")
        result = classify_project(t)
        self.assertEqual(result, ["pantheon-config"])


# ---------------------------------------------------------------------------
# TestResolvedTicketGathering — gather_resolved_tickets().
# ---------------------------------------------------------------------------


class TestResolvedTicketGathering(unittest.TestCase):
    def _window(self, anchor_iso: str) -> tuple[datetime, datetime]:
        anchor = datetime.fromisoformat(anchor_iso.replace("Z", "+00:00"))
        start = anchor.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=5)
        return start, end

    def test_ticket_in_window_included(self):
        """A ticket resolved inside the window is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_file(
                tickets_root, "T-0001", labels=["b9"],
                resolve_ts="2026-05-04T21:30:00Z",
            )
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].fields["id"], "T-0001")

    def test_ticket_outside_window_excluded(self):
        """A ticket resolved before the window is not returned."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_file(
                tickets_root, "T-0001", labels=["b9"],
                resolve_ts="2026-05-04T15:00:00Z",
            )
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(len(tickets), 0)

    def test_ticket_at_exact_window_start_included(self):
        """Ticket resolved exactly at window_start (boundary inclusive)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_file(
                tickets_root, "T-0001", labels=[],
                resolve_ts="2026-05-04T20:00:00Z",
            )
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(len(tickets), 1)

    def test_ticket_at_exact_window_end_excluded(self):
        """Ticket resolved exactly at window_end (boundary exclusive)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            _write_ticket_file(
                tickets_root, "T-0001", labels=[],
                resolve_ts="2026-05-04T22:00:00Z",
            )
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(len(tickets), 0)

    def test_empty_done_dir_returns_empty(self):
        """No done tickets → empty list (no exception)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            (tickets_root / "done").mkdir(parents=True)
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(tickets, [])

    def test_missing_done_dir_returns_empty(self):
        """Missing done/ directory → empty list (no exception)."""
        with tempfile.TemporaryDirectory() as tmp:
            tickets_root = Path(tmp)
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.TICKETS_ROOT", tickets_root):
                tickets = gather_resolved_tickets(window_start, window_end)
        self.assertEqual(tickets, [])


# ---------------------------------------------------------------------------
# TestMarkdownFormat — render_digest().
# ---------------------------------------------------------------------------


class TestMarkdownFormat(unittest.TestCase):
    def _make_arc_map(self) -> dict[str, str]:
        return {
            "B9": "Per-agent inbox auto-consumer",
            "B10": "Activity digest system-pulse",
        }

    def test_non_empty_window_contains_header(self):
        """Rendered digest contains the window header line."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 21, 30, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"], resolve_ts="2026-05-04T20:30:00Z"),
        ]
        arc_map = self._make_arc_map()
        out = render_digest(tickets, arc_map, window_start, window_end)
        self.assertIn("2026-05-04T20:00:00Z", out)
        self.assertIn("2026-05-04T21:30:00Z", out)

    def test_non_empty_window_contains_arc_shortform(self):
        """Rendered digest contains B<N>(short title) for touched arcs."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"], resolve_ts="2026-05-04T20:30:00Z"),
            _make_ticket("T-0063", labels=["b10"], resolve_ts="2026-05-04T21:00:00Z"),
        ]
        arc_map = self._make_arc_map()
        out = render_digest(tickets, arc_map, window_start, window_end)
        self.assertIn("B9", out)
        self.assertIn("B10", out)
        self.assertIn("Per-agent inbox auto-consumer", out)

    def test_non_empty_window_contains_project_section(self):
        """Rendered digest contains a project counts section."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"]),
        ]
        arc_map = self._make_arc_map()
        out = render_digest(tickets, arc_map, window_start, window_end)
        self.assertIn("pantheon-config", out)

    def test_non_empty_window_contains_footer_count(self):
        """Footer contains ticket + arc counts."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"]),
            _make_ticket("T-0063", labels=["b10"]),
        ]
        arc_map = self._make_arc_map()
        out = render_digest(tickets, arc_map, window_start, window_end)
        self.assertIn("2 ticket", out)

    def test_quiet_window_one_line_message(self):
        """Zero tickets → single-line no-activity message."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        out = render_digest([], {}, window_start, window_end)
        self.assertIn("No notable activity", out)
        self.assertIn("2026-05-04T20:00:00Z", out)
        # Should be a short message — fewer than 5 lines.
        self.assertLess(len(out.strip().splitlines()), 5)

    def test_ticket_title_markdown_chars_stripped(self):
        """Ticket titles with Markdown special chars are sanitized."""
        fields = {
            "id": "T-0099",
            "created": "2026-05-04T10:00:00Z",
            "requester": "orchestrator",
            "target": "orchestrator",
            "state": "done",
            "parent_ticket": None,
            "depends_on": [],
            "labels": ["b9"],
            "stale_after": "7d",
        }
        body = (
            "# Title *with* `special` _chars_ [link]\n\n"
            "## Transition log\n\n"
            "- 2026-05-04T20:30:00Z [ho] resolved; done.\n"
        )
        t = Ticket(path=Path("/fake/T-0099.md"), fields=fields, body=body,
                   title="Title *with* `special` _chars_ [link]")
        arc_map = self._make_arc_map()
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        out = render_digest([t], arc_map, window_start, window_end)
        # No unescaped special chars from the title in the output.
        self.assertNotIn("*with*", out)
        self.assertNotIn("`special`", out)
        self.assertNotIn("_chars_", out)

    def test_arc_with_no_tickets_in_window_not_shown(self):
        """An arc in arc_map that has no resolved tickets is not listed."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"]),
        ]
        arc_map = self._make_arc_map()  # includes both B9 and B10
        out = render_digest(tickets, arc_map, window_start, window_end)
        # B10 should not appear since no tickets touch it.
        self.assertNotIn("B10", out)

    def test_ticket_under_both_project_and_arc(self):
        """A ticket appears under its arc section and in project count."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        t = _make_ticket(
            "T-0051",
            labels=["b9"],
            resolve_line="resolved; bridge endpoint landed at pantheon-bridge@abc.",
        )
        arc_map = self._make_arc_map()
        out = render_digest([t], arc_map, window_start, window_end)
        self.assertIn("pantheon-bridge", out)
        self.assertIn("B9", out)

    def test_no_malformed_asterisks(self):
        """Rendered output has balanced asterisks (no broken Telegram Markdown)."""
        window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 5, 4, 22, 0, 0, tzinfo=timezone.utc)
        tickets = [
            _make_ticket("T-0061", labels=["b9"]),
        ]
        arc_map = self._make_arc_map()
        out = render_digest(tickets, arc_map, window_start, window_end)
        # Count asterisks — must be even for balanced pairs.
        self.assertEqual(out.count("*") % 2, 0)


# ---------------------------------------------------------------------------
# TestTelegramPost — mock urllib.
# ---------------------------------------------------------------------------


class TestTelegramPost(unittest.TestCase):
    def _make_env_file(self, tmp: str) -> Path:
        p = Path(tmp) / ".env"
        p.write_text("TELEGRAM_BOT_TOKEN=test-token-123\n", encoding="utf-8")
        return p

    def test_post_url_and_body_shape(self):
        """POST goes to the correct sendMessage URL with required fields."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._make_env_file(tmp)
            captured_requests = []

            def fake_urlopen(req, timeout=None):
                captured_requests.append(req)
                resp = MagicMock()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                resp.status = 200
                return resp

            with patch("tools.activity_digest.TELEGRAM_ENV", env_path):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    send_telegram("hello world")

        self.assertEqual(len(captured_requests), 1)
        req = captured_requests[0]
        self.assertIn("/sendMessage", req.full_url)
        self.assertIn("test-token-123", req.full_url)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "456474197")
        self.assertEqual(body["text"], "hello world")
        self.assertEqual(body["parse_mode"], "Markdown")

    def test_no_token_logs_warning_no_raise(self):
        """Missing token: function returns without raising."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"  # does not exist
            with patch("tools.activity_digest.TELEGRAM_ENV", env_path):
                # Should not raise.
                send_telegram("test")


# ---------------------------------------------------------------------------
# TestLedgerWrite.
# ---------------------------------------------------------------------------


class TestLedgerWrite(unittest.TestCase):
    def test_non_empty_fire_appends_ledger_entry(self):
        """Non-empty fire appends a JSONL entry with correct action field."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "2026.jsonl"
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.LEDGER_PATH", ledger_path):
                write_ledger_entry(
                    window_start=window_start,
                    ticket_count=3,
                    arc_count=2,
                    agent="orchestrator",
                )
            lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["action"], "activity_digest_2026-05-04T20:00:00Z")
        self.assertEqual(entry["agent"], "orchestrator")
        self.assertIn("id", entry)
        self.assertIn("ts", entry)

    def test_quiet_fire_does_not_write_ledger(self):
        """main() with zero resolved tickets must NOT call write_ledger_entry.

        The guard lives in main(), not in write_ledger_entry itself, so this
        test exercises the full main() call path with gather_resolved_tickets
        patched to return [].
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "2026.jsonl"
            env_path = Path(tmp) / ".env"
            env_path.write_text("TELEGRAM_BOT_TOKEN=fake\n", encoding="utf-8")
            with (
                # T-2898: pull_repo_or_warn MUST be patched in every main()-path
                # test — unmocked it runs a REAL `git fetch` + `merge --ff-only
                # origin/main` against whatever repo the suite runs in, which
                # fast-forwards a checked-out agent-worktree branch mid-run (the
                # silent base-drift that hit the T-2841 build) and makes the
                # suite network-dependent.
                patch("tools.activity_digest.pull_repo_or_warn"),
                patch("tools.activity_digest.gather_resolved_tickets", return_value=[]),
                patch("tools.activity_digest.send_telegram"),
                patch("tools.activity_digest.write_ledger_entry") as mock_ledger,
                patch("tools.activity_digest.LEDGER_PATH", ledger_path),
                patch("tools.activity_digest.LOG_PATH", Path(tmp) / "digest.log"),
                patch("tools.activity_digest.TELEGRAM_ENV", env_path),
            ):
                from tools.activity_digest import main
                main(["--agent", "orchestrator"])
            mock_ledger.assert_not_called()
            # Belt-and-suspenders: ledger file must not have been created.
            self.assertFalse(ledger_path.exists())

    def test_routes_to_legacy_when_present(self):
        """T-0148 Phase 2: LEDGER_PATH=None + legacy 2026.jsonl present in
        LEDGER_DIR → write lands in the legacy file (behavior-preserving)."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp) / "ledger"
            ledger_dir.mkdir()
            legacy = ledger_dir / "2026.jsonl"
            legacy.write_text("")
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with (
                patch("tools.activity_digest.LEDGER_PATH", None),
                patch("tools.activity_digest.LEDGER_DIR", ledger_dir),
            ):
                write_ledger_entry(
                    window_start=window_start, ticket_count=1, arc_count=1,
                    agent="orchestrator",
                )
            self.assertEqual(len(legacy.read_text().strip().splitlines()), 1)

    def test_routes_to_week_shard_when_legacy_absent(self):
        """T-0148 Phase 2: LEDGER_PATH=None + no legacy file → write lands in
        the current ISO-week shard and ensure_segment_dir mkdirs the parent."""
        from tools.ledger_shard import current_write_target
        with tempfile.TemporaryDirectory() as tmp:
            ledger_dir = Path(tmp) / "ledger"
            ledger_dir.mkdir()
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with (
                patch("tools.activity_digest.LEDGER_PATH", None),
                patch("tools.activity_digest.LEDGER_DIR", ledger_dir),
            ):
                write_ledger_entry(
                    window_start=window_start, ticket_count=1, arc_count=1,
                    agent="orchestrator",
                )
            expected = current_write_target(ledger_dir, datetime.now(timezone.utc))
            self.assertTrue(expected.parent.is_dir())
            self.assertEqual(len(expected.read_text().strip().splitlines()), 1)

    def test_ledger_entry_schema_fields_present(self):
        """Ledger entry contains all required schema fields from ORG-001."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "2026.jsonl"
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.LEDGER_PATH", ledger_path):
                write_ledger_entry(
                    window_start=window_start,
                    ticket_count=2,
                    arc_count=1,
                    agent="orchestrator",
                )
            entry = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        required_fields = {"id", "ts", "agent", "action", "rationale", "justifies_via"}
        for field in required_fields:
            self.assertIn(field, entry, f"Missing ledger field: {field}")


# ---------------------------------------------------------------------------
# TestDryRun — --dry-run skips both Telegram POST and ledger write.
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    def _non_empty_ticket(self) -> Ticket:
        return _make_ticket(
            "T-0061",
            labels=["b9"],
            resolve_ts="2026-05-04T20:30:00Z",
        )

    def test_dry_run_skips_telegram_and_ledger(self):
        """--dry-run: send_telegram and write_ledger_entry are NEVER called."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "digest.log"
            with (
                patch("tools.activity_digest.pull_repo_or_warn"),  # T-2898: never real git in tests
                patch(
                    "tools.activity_digest.gather_resolved_tickets",
                    return_value=[self._non_empty_ticket()],
                ),
                patch("tools.activity_digest.send_telegram") as mock_send,
                patch("tools.activity_digest.write_ledger_entry") as mock_ledger,
                patch("tools.activity_digest.LOG_PATH", log_path),
            ):
                from tools.activity_digest import main
                main(["--agent", "orchestrator", "--dry-run"])
            mock_send.assert_not_called()
            mock_ledger.assert_not_called()

    def test_dry_run_prints_rendered_markdown(self):
        """--dry-run: rendered markdown is printed to stdout."""
        import io
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "digest.log"
            with (
                patch("tools.activity_digest.pull_repo_or_warn"),  # T-2898: never real git in tests
                patch(
                    "tools.activity_digest.gather_resolved_tickets",
                    return_value=[self._non_empty_ticket()],
                ),
                patch("tools.activity_digest.send_telegram"),
                patch("tools.activity_digest.write_ledger_entry"),
                patch("tools.activity_digest.LOG_PATH", log_path),
            ):
                from tools.activity_digest import main
                import sys
                captured = io.StringIO()
                old_stdout = sys.stdout
                sys.stdout = captured
                try:
                    main(["--agent", "orchestrator", "--dry-run"])
                finally:
                    sys.stdout = old_stdout
            output = captured.getvalue()
        self.assertIn("Activity digest", output)
        self.assertIn("B9", output)


# ---------------------------------------------------------------------------
# TestLogging — write_log().
# ---------------------------------------------------------------------------


class TestLogging(unittest.TestCase):
    def test_non_empty_log_line_format(self):
        """Non-quiet fire: log line contains ISO ts, agent, window-start, counts."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "activity_digest.log"
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.LOG_PATH", log_path):
                write_log(
                    agent="orchestrator",
                    window_start=window_start,
                    ticket_count=3,
                    arc_count=2,
                )
            line = log_path.read_text(encoding="utf-8").strip()
        # Format: <ISO> orchestrator 2026-05-04T20:00:00Z 3tickets 2arcs
        self.assertIn("orchestrator", line)
        self.assertIn("2026-05-04T20:00:00Z", line)
        self.assertIn("3", line)
        self.assertIn("2", line)

    def test_quiet_log_line_format(self):
        """Quiet fire: log line contains 'quiet' marker."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "activity_digest.log"
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.LOG_PATH", log_path):
                write_log(
                    agent="orchestrator",
                    window_start=window_start,
                    ticket_count=0,
                    arc_count=0,
                )
            line = log_path.read_text(encoding="utf-8").strip()
        self.assertIn("quiet", line)
        self.assertIn("orchestrator", line)

    def test_log_dir_created_if_missing(self):
        """Log write creates parent directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "subdir" / "activity_digest.log"
            window_start = datetime(2026, 5, 4, 20, 0, 0, tzinfo=timezone.utc)
            with patch("tools.activity_digest.LOG_PATH", log_path):
                write_log(agent="recovery", window_start=window_start,
                          ticket_count=0, arc_count=0)
            self.assertTrue(log_path.exists())


# ---------------------------------------------------------------------------
# TestStripMd — _strip_md() helper.
# ---------------------------------------------------------------------------


class TestStripMd(unittest.TestCase):
    def test_strips_asterisks(self):
        self.assertEqual(_strip_md("hello *world*"), "hello world")

    def test_strips_underscores(self):
        self.assertEqual(_strip_md("hello _world_"), "hello world")

    def test_strips_backticks(self):
        self.assertEqual(_strip_md("hello `code`"), "hello code")

    def test_strips_brackets(self):
        self.assertEqual(_strip_md("hello [link] end"), "hello link end")

    def test_plain_text_unchanged(self):
        self.assertEqual(_strip_md("plain text"), "plain text")


# ---------------------------------------------------------------------------
# TestLoadBacklogArcs — load_backlog_arcs().
# ---------------------------------------------------------------------------


class TestLoadBacklogArcs(unittest.TestCase):
    def test_parses_open_arc(self):
        """Open arc heading parsed correctly."""
        content = "## B2 — Cost monitoring (reshaped from ceilings)\n\nsome body\n"
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "BACKLOG.md"
            backlog.write_text(content, encoding="utf-8")
            with patch("tools.activity_digest.BACKLOG_PATH", backlog):
                arcs = load_backlog_arcs()
        self.assertIn("B2", arcs)
        self.assertIn("Cost monitoring", arcs["B2"])

    def test_parses_closed_arc(self):
        """Closed arc heading (with checkmark suffix) parsed; CLOSED part not in title."""
        content = "## B1 — Decision ledger schema and storage  ✓ CLOSED 2026-04-28\n"
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "BACKLOG.md"
            backlog.write_text(content, encoding="utf-8")
            with patch("tools.activity_digest.BACKLOG_PATH", backlog):
                arcs = load_backlog_arcs()
        self.assertIn("B1", arcs)
        self.assertNotIn("CLOSED", arcs["B1"])

    def test_shortform_truncated_to_six_words(self):
        """Arc title with many words is truncated to first 6."""
        long_title = "one two three four five six seven eight"
        content = f"## B99 — {long_title}\n"
        with tempfile.TemporaryDirectory() as tmp:
            backlog = Path(tmp) / "BACKLOG.md"
            backlog.write_text(content, encoding="utf-8")
            with patch("tools.activity_digest.BACKLOG_PATH", backlog):
                arcs = load_backlog_arcs()
        words = arcs["B99"].split()
        self.assertLessEqual(len(words), 6)
        self.assertNotIn("seven", arcs["B99"])


class TestPullRepoOrWarn(unittest.TestCase):
    """pull_repo_or_warn fail-open semantics for activity_digest — never raises, never blocks."""

    def test_pull_success_silent(self):
        import io
        from tools.activity_digest import pull_repo_or_warn
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            pull_repo_or_warn("activity_digest")  # no exception
        # T-0218: pull_repo_or_warn now fetches + merges (resilient to FETCH_HEAD pollution).
        self.assertEqual(mock_sub.run.call_count, 2)
        first_args, _ = mock_sub.run.call_args_list[0]
        self.assertEqual(first_args[0], ["git", "fetch", "origin", "main"])
        second_args, _ = mock_sub.run.call_args_list[1]
        self.assertEqual(second_args[0], ["git", "merge", "--ff-only", "refs/remotes/origin/main"])

    def test_pull_nonzero_proceeds(self):
        import io
        import contextlib
        from tools.activity_digest import pull_repo_or_warn
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout="", stderr="fatal: refusing to merge unrelated histories\n"
            )
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                pull_repo_or_warn("activity_digest")  # must not raise
            self.assertIn("fetch failed", buf.getvalue())
            self.assertIn("refusing to merge", buf.getvalue())

    def test_pull_timeout_proceeds(self):
        import io
        import contextlib
        import subprocess as real_sub
        from tools.activity_digest import pull_repo_or_warn
        with patch("tools._git_utils.subprocess") as mock_sub:
            mock_sub.TimeoutExpired = real_sub.TimeoutExpired
            mock_sub.run.side_effect = real_sub.TimeoutExpired(cmd="git", timeout=30)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                pull_repo_or_warn("activity_digest")  # must not raise
            self.assertIn("fetch skipped", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
