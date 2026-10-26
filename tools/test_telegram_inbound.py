"""Tests for tools/telegram_inbound.py (T-4205). Fixtures mirror the three real
jsonl shapes seen on a live orchestrator session (2026-09-29), plus look-alikes that must
NOT count as the operator being present."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import telegram_inbound as ti  # noqa: E402

NOW = 1_790_640_000  # fixed "now" for the pure tests


def iso(unix: float) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def tag(ts_unix: float, body: str = "hello") -> str:
    return (f'<channel source="plugin:telegram:telegram" chat_id="424242424" message_id="1" '
            f'user="example-org" user_id="424242424" ts="{iso(ts_unix)}">\n{body}\n</channel>')


def user_line(ts_unix: float) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": tag(ts_unix)}})


def user_blocks_line(ts_unix: float) -> str:
    return json.dumps({"type": "user", "message": {"role": "user",
                       "content": [{"type": "text", "text": tag(ts_unix)}]}})


def queued_line(ts_unix: float) -> str:
    return json.dumps({"type": "attachment", "attachment": {"type": "queued_command", "prompt": tag(ts_unix)}})


def enqueue_line(ts_unix: float) -> str:
    return json.dumps({"type": "queue-operation", "operation": "enqueue", "content": tag(ts_unix)})


class TestShapes(unittest.TestCase):
    def test_each_real_shape_yields_its_ts(self):
        for make in (user_line, user_blocks_line, queued_line, enqueue_line):
            with self.subTest(shape=make.__name__):
                self.assertEqual(ti.inbound_ts_from_line(make(NOW - 60)), NOW - 60)

    def test_quoted_tag_in_assistant_turn_is_ignored(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": tag(NOW - 10)}]}})
        self.assertIsNone(ti.inbound_ts_from_line(line))

    def test_instructions_delta_attachment_is_ignored(self):
        line = json.dumps({"type": "attachment", "attachment": {
            "type": "mcp_instructions_delta", "addedBlocks": [tag(NOW - 10)]}})
        self.assertIsNone(ti.inbound_ts_from_line(line))

    def test_user_text_that_only_mentions_the_tag_is_ignored(self):
        # e.g. a CLAUDE.md passage quoting the tag mid-sentence, or a pasted log
        line = json.dumps({"type": "user", "message": {"content": "When a message arrives via " + tag(NOW - 10)}})
        self.assertIsNone(ti.inbound_ts_from_line(line))

    def test_dequeue_operation_is_ignored(self):
        line = json.dumps({"type": "queue-operation", "operation": "remove", "content": tag(NOW - 10)})
        self.assertIsNone(ti.inbound_ts_from_line(line))

    def test_tag_without_ts_and_malformed_json(self):
        no_ts = json.dumps({"type": "user", "message": {"content": '<channel source="plugin:telegram:telegram" chat_id="...">x'}})
        self.assertIsNone(ti.inbound_ts_from_line(no_ts))
        self.assertIsNone(ti.inbound_ts_from_line('{"type": "user", ' + ti.CHANNEL_TAG))


class TestLatest(unittest.TestCase):
    def test_max_across_lines(self):
        lines = [user_line(NOW - 900), queued_line(NOW - 30), enqueue_line(NOW - 300)]
        self.assertEqual(ti.latest_inbound_ts_from_lines(lines, now=NOW), NOW - 30)

    def test_implausibly_future_ts_is_ignored(self):
        lines = [user_line(NOW - 900), user_line(NOW + ti.MAX_FUTURE_SKEW_SECONDS + 60)]
        self.assertEqual(ti.latest_inbound_ts_from_lines(lines, now=NOW), NOW - 900)

    def test_none_when_no_inbound(self):
        self.assertIsNone(ti.latest_inbound_ts_from_lines(['{"type": "assistant"}'], now=NOW))


class TestRecentInboundTs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.proj = self.root / "-home-agent"
        self.proj.mkdir()
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, lines: list[str], mtime: float | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_recent_session_inbound_found(self):
        self._write("-home-agent/s1.jsonl", ['{"type": "summary"}', queued_line(self.now - 60)])
        self.assertEqual(ti.recent_inbound_ts(self.root, 300, now=self.now), int(self.now - 60))

    def test_files_not_written_within_window_are_not_read(self):
        self._write("-home-agent/old.jsonl", [user_line(self.now - 60)], mtime=self.now - 3600)
        self.assertIsNone(ti.recent_inbound_ts(self.root, 300, now=self.now))

    def test_subagent_transcripts_are_not_scanned(self):
        self._write("-home-agent/s1/subagents/a.jsonl", [user_line(self.now - 60)])
        self.assertIsNone(ti.recent_inbound_ts(self.root, 300, now=self.now))

    def test_latest_across_projects(self):
        self._write("-home-agent/s1.jsonl", [user_line(self.now - 200)])
        self._write("-other-project/s2.jsonl", [enqueue_line(self.now - 20)])
        self.assertEqual(ti.recent_inbound_ts(self.root, 300, now=self.now), int(self.now - 20))

    def test_tail_read_drops_the_partial_first_line(self):
        p = self._write("-home-agent/big.jsonl", [user_line(self.now - 100), '{"type": "x", "pad": "' + "y" * 5000 + '"}', queued_line(self.now - 10)])
        lines = ti._tail_lines(p, max_bytes=len(queued_line(self.now - 10)) + 3000)
        self.assertEqual(ti.latest_inbound_ts_from_lines(lines, now=self.now), int(self.now - 10))
        self.assertFalse(any(l.startswith('{"type": "user"') for l in lines))

    def test_missing_root_raises_oserror(self):
        with self.assertRaises(OSError):
            ti.recent_inbound_ts(self.root / "nope", 300, now=self.now)


if __name__ == "__main__":
    unittest.main()
