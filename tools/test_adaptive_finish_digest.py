#!/usr/bin/env python3
"""Tests for tools/adaptive_finish_digest.py (T-0907)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass the canonical smoke
# `python3 -m unittest tools.test_adaptive_finish_digest` AND a standalone run.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import adaptive_finish_digest as afd  # noqa: E402


def _write_log(state_dir: Path, records: list[dict]) -> None:
    p = state_dir / "adaptive_finish_log.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


class LoadHitsTests(unittest.TestCase):
    def test_missing_log_is_empty(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(afd.load_hits(Path(t) / "nope.jsonl"), [])

    def test_skips_malformed_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "adaptive_finish_log.jsonl"
            p.write_text('{"ts": 1, "agent_type": "impl"}\n\nnot json\n{"ts": 2}\n')
            hits = afd.load_hits(p)
            self.assertEqual([h["ts"] for h in hits], [1, 2])


class WindowTests(unittest.TestCase):
    def test_filters_by_since_and_excludes_non_numeric_ts(self):
        hits = [{"ts": 100}, {"ts": 200}, {"ts": None}, {"no_ts": 1}, {"ts": "x"}]
        self.assertEqual(afd.window_hits(hits, since_ts=150), [{"ts": 200}])

    def test_boundary_is_inclusive(self):
        self.assertEqual(afd.window_hits([{"ts": 150}], since_ts=150), [{"ts": 150}])


class SummarizeTests(unittest.TestCase):
    def test_counts_and_samples(self):
        hits = [
            {"ts": 1, "agent_type": "implementer", "matched_phrase": "I will",
             "message_excerpt": "I will write the test", "false_positive_marked": True},
            {"ts": 2, "agent_type": "implementer", "matched_phrase": "I'll",
             "message_excerpt": "I'll do it"},
            {"ts": 3, "agent_type": "reviewer", "matched_phrase": "I will",
             "message_excerpt": "I will check"},
        ]
        s = afd.summarize(hits)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_agent"], {"implementer": 2, "reviewer": 1})
        self.assertEqual(s["by_phrase"]["I will"], 2)
        self.assertEqual(s["false_positive_marked"], 1)
        self.assertEqual(len(s["samples"]), 3)

    def test_missing_fields_default_safely(self):
        s = afd.summarize([{"ts": 1}])
        self.assertEqual(s["by_agent"], {"unknown": 1})
        self.assertEqual(s["by_phrase"], {"?": 1})
        self.assertEqual(s["false_positive_marked"], 0)


class FormatTests(unittest.TestCase):
    def test_zero_hits_message(self):
        out = afd.format_digest(afd.summarize([]), days=7)
        self.assertIn("hits: 0", out)
        self.assertIn("last 7d", out)

    def test_populated_digest_has_breakdowns(self):
        hits = [{"ts": 1, "agent_type": "impl", "matched_phrase": "I will",
                 "message_excerpt": "I will do X"}]
        out = afd.format_digest(afd.summarize(hits), days=7)
        self.assertIn("hits: 1", out)
        self.assertIn("by agent", out)
        self.assertIn("impl", out)


class BuildDigestTests(unittest.TestCase):
    def test_window_applied_with_injected_now(self):
        with tempfile.TemporaryDirectory() as t:
            # now=1_000_000; 7d window → since=395200. Only ts >= that count.
            _write_log(Path(t), [
                {"ts": 100, "agent_type": "old", "matched_phrase": "I will"},      # excluded
                {"ts": 999_000, "agent_type": "impl", "matched_phrase": "I will"},  # included
            ])
            count, digest = afd.build_digest(days=7, state_dir=t, now=1_000_000)
            self.assertEqual(count, 1)
            self.assertIn("impl", digest)
            self.assertNotIn("old", digest)


class MainTests(unittest.TestCase):
    def test_skip_on_empty_returns_0_without_send(self):
        sent = []
        orig = afd.send_telegram
        afd.send_telegram = lambda *a, **k: sent.append(a) or True
        try:
            with tempfile.TemporaryDirectory() as t:
                rc = afd.main(["--state-dir", t])  # no log → 0 hits → skip
        finally:
            afd.send_telegram = orig
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [], "must not send when window is empty and --always absent")

    def test_dry_run_prints_and_does_not_send(self):
        sent = []
        orig = afd.send_telegram
        afd.send_telegram = lambda *a, **k: sent.append(a) or True
        try:
            with tempfile.TemporaryDirectory() as t:
                _write_log(Path(t), [{"ts": int(2e9), "agent_type": "impl",
                                      "matched_phrase": "I will", "message_excerpt": "x"}])
                # huge ts → always in window regardless of real clock
                rc = afd.main(["--state-dir", t, "--days", "36500", "--dry-run"])
        finally:
            afd.send_telegram = orig
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [], "--dry-run must not send")

    def test_always_sends_even_when_empty(self):
        sent = []
        orig_send = afd.send_telegram
        orig_tok = afd.read_telegram_token
        afd.send_telegram = lambda token, chat, text: sent.append((chat, text)) or True
        afd.read_telegram_token = lambda path: "fake-token"
        try:
            with tempfile.TemporaryDirectory() as t:
                rc = afd.main(["--state-dir", t, "--always", "--chat-id", "999"])
        finally:
            afd.send_telegram = orig_send
            afd.read_telegram_token = orig_tok
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "999")
        self.assertIn("hits: 0", sent[0][1])


if __name__ == "__main__":
    unittest.main()
