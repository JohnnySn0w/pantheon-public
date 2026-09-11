#!/usr/bin/env python3
"""Tests for tools/window_executor.py (T-2876 increment 1, report-only)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.window_executor import (
    AUTO_DRAIN_LABEL,
    ENVELOPE_AUTO_DRAIN,
    ENVELOPE_RESERVED,
    WindowItem,
    execute_item,
    format_report,
    main,
    scan_window_queue,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _write_ticket(
    root: Path,
    state: str,
    tid: str,
    *,
    anchor: str | None,
    not_before: str | None = None,
    labels: list[str] | None = None,
    target: str = "recovery",
    title: str = "a window task",
) -> Path:
    d = root / state
    d.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: {tid}",
        "created: 2026-08-01T00:00:00Z",
        "requester: operator",
        f"target: {target}",
        f"state: {state}",
    ]
    if labels is not None:
        fm.append("labels: [" + ", ".join(labels) + "]")
    if not_before is not None:
        fm.append(f"not_before: {not_before}")
    if anchor is not None:
        fm.append(f"not_before_anchor: {anchor}")
    fm.append("---")
    body = f"\n# {title}\n\n## Ask\n\nbody text\n"
    p = d / f"{tid}.md"
    p.write_text("\n".join(fm) + body, encoding="utf-8")
    return p


class TestScanQueue(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_window_anchor_is_queued(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0001",
            anchor="event:window:acp-rollout",
            not_before="2026-08-01T00:00:00Z",
        )
        items = scan_window_queue(self.root, now=NOW)
        self.assertEqual([i.tid for i in items], ["T-0001"])
        self.assertEqual(items[0].param, "acp-rollout")

    def test_cadence_anchor_is_excluded(self) -> None:
        """event:cadence: is a periodic re-check, never a maintenance window."""
        _write_ticket(
            self.root, "inbox", "T-0002",
            anchor="event:cadence:disk-reclaim-recheck",
            not_before="2026-08-01T00:00:00Z",
        )
        self.assertEqual(scan_window_queue(self.root, now=NOW), [])

    def test_operator_anchor_is_excluded(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0003",
            anchor="operator:tg:5665",
            not_before="2026-08-01T00:00:00Z",
        )
        self.assertEqual(scan_window_queue(self.root, now=NOW), [])

    def test_no_anchor_is_excluded(self) -> None:
        _write_ticket(self.root, "active", "T-0004", anchor=None)
        self.assertEqual(scan_window_queue(self.root, now=NOW), [])

    def test_done_state_not_scanned(self) -> None:
        _write_ticket(
            self.root, "done", "T-0005",
            anchor="event:window:x", not_before="2026-08-01T00:00:00Z",
        )
        self.assertEqual(scan_window_queue(self.root, now=NOW), [])

    def test_both_inbox_and_active_scanned(self) -> None:
        _write_ticket(self.root, "inbox", "T-0006", anchor="event:window:a")
        _write_ticket(self.root, "active", "T-0007", anchor="event:window:b")
        self.assertEqual(
            sorted(i.tid for i in scan_window_queue(self.root, now=NOW)),
            ["T-0006", "T-0007"],
        )


class TestEnvelopeClassification(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_is_reserved_failsafe(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0010",
            anchor="event:window:x", labels=["acp-bridge", "rollout"],
        )
        item = scan_window_queue(self.root, now=NOW)[0]
        self.assertEqual(item.envelope, ENVELOPE_RESERVED)

    def test_no_labels_is_reserved(self) -> None:
        _write_ticket(self.root, "inbox", "T-0011", anchor="event:window:x")
        item = scan_window_queue(self.root, now=NOW)[0]
        self.assertEqual(item.envelope, ENVELOPE_RESERVED)

    def test_auto_drain_label_opts_in(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0012",
            anchor="event:window:x", labels=["infra", AUTO_DRAIN_LABEL],
        )
        item = scan_window_queue(self.root, now=NOW)[0]
        self.assertEqual(item.envelope, ENVELOPE_AUTO_DRAIN)


class TestWindowOpen(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_past_not_before_is_open(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0020",
            anchor="event:window:x", not_before="2026-08-01T00:00:00Z",
        )
        self.assertTrue(scan_window_queue(self.root, now=NOW)[0].window_open)

    def test_future_not_before_is_closed(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0021",
            anchor="event:window:x", not_before="2026-09-01T00:00:00Z",
        )
        self.assertFalse(scan_window_queue(self.root, now=NOW)[0].window_open)

    def test_missing_not_before_is_open(self) -> None:
        """A window anchor with no timing gate ⇒ the anchor itself is the request."""
        _write_ticket(self.root, "inbox", "T-0022", anchor="event:window:x")
        self.assertTrue(scan_window_queue(self.root, now=NOW)[0].window_open)

    def test_unparseable_not_before_is_closed_failsafe(self) -> None:
        _write_ticket(
            self.root, "inbox", "T-0023",
            anchor="event:window:x", not_before="not-a-date",
        )
        self.assertFalse(scan_window_queue(self.root, now=NOW)[0].window_open)


class TestExecuteGate(unittest.TestCase):
    def test_execute_item_refuses(self) -> None:
        item = WindowItem(
            tid="T-9999", title="x", state="inbox", target="recovery",
            anchor="event:window:x", param="x", not_before=None,
        )
        with self.assertRaises(NotImplementedError):
            execute_item(item)

    def test_cli_execute_flag_refused(self) -> None:
        self.assertEqual(main(["--execute"]), 2)


class TestReport(unittest.TestCase):
    def test_empty_queue_report(self) -> None:
        self.assertIn("queue empty", format_report([]))

    def test_disposition_open_auto_drain(self) -> None:
        item = WindowItem(
            tid="T-1", title="x", state="inbox", target="recovery",
            anchor="event:window:x", param="x", not_before=None,
            window_open=True, envelope=ENVELOPE_AUTO_DRAIN,
        )
        self.assertIn("AUTO-DRAIN", item.disposition)

    def test_disposition_reserved_routes_to_operator(self) -> None:
        item = WindowItem(
            tid="T-2", title="x", state="inbox", target="recovery",
            anchor="event:window:x", param="x", not_before=None,
            window_open=True, envelope=ENVELOPE_RESERVED,
        )
        self.assertIn("the operator", item.disposition)


if __name__ == "__main__":
    unittest.main()
